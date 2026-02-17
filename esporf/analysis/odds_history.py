"""Odds history tracking for CLV analysis and steam move detection.

Stores timestamped snapshots of odds so we can detect:
- Closing Line Value (CLV): did you beat the closing line?
- Steam moves: sudden sharp line moves indicating sharp action
- Line trends: which direction is the market moving?
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict

from esporf.config import settings
from esporf.models import MarketType, Match, OddsLine, OddsSnapshot, Outcome

logger = logging.getLogger(__name__)


class OddsHistory:
    """In-memory + file-backed odds history for tracking line movements."""

    def __init__(self):
        # Key: (match_id, market, outcome, sportsbook) -> list of snapshots
        self._history: dict[tuple, list[OddsSnapshot]] = defaultdict(list)
        self._data_file = os.path.join(settings.data_dir, "odds_history.json")

    def record(self, match: Match) -> None:
        """Record current odds for a match as a new snapshot."""
        now = time.time()
        for odds_line in match.odds:
            key = self._key(match.match_id, odds_line)
            snapshot = OddsSnapshot(
                match_id=match.match_id,
                market=odds_line.market,
                outcome=odds_line.outcome,
                sportsbook=odds_line.sportsbook,
                odds=odds_line.odds,
                line=odds_line.line,
                timestamp=now,
            )
            self._history[key].append(snapshot)

    def get_history(
        self,
        match_id: str,
        market: MarketType,
        outcome: Outcome,
        sportsbook: str | None = None,
    ) -> list[OddsSnapshot]:
        """Get historical odds snapshots, optionally filtered by sportsbook."""
        results: list[OddsSnapshot] = []
        for key, snapshots in self._history.items():
            k_match, k_market, k_outcome, k_book = key
            if k_match != match_id:
                continue
            if k_market != market or k_outcome != outcome:
                continue
            if sportsbook and k_book != sportsbook:
                continue
            results.extend(snapshots)
        results.sort(key=lambda s: s.timestamp)
        return results

    def get_opening_odds(
        self, match_id: str, market: MarketType, outcome: Outcome, sportsbook: str
    ) -> float | None:
        """Get the first recorded odds (opening line) for a selection."""
        history = self.get_history(match_id, market, outcome, sportsbook)
        return history[0].odds if history else None

    def get_latest_odds(
        self, match_id: str, market: MarketType, outcome: Outcome, sportsbook: str
    ) -> float | None:
        """Get the most recent recorded odds for a selection."""
        history = self.get_history(match_id, market, outcome, sportsbook)
        return history[-1].odds if history else None

    def detect_steam_moves(
        self,
        match_id: str,
        market: MarketType,
        outcome: Outcome,
        threshold: float | None = None,
        window_seconds: int | None = None,
    ) -> list[dict]:
        """Detect sharp/sudden line movements (steam moves).

        A steam move is when odds shift significantly within a short time
        window, indicating sharp money has come in on one side.

        Returns a list of detected steam moves with details.
        """
        threshold = threshold or settings.steam_move_threshold
        window = window_seconds or settings.steam_move_window_seconds
        steam_moves: list[dict] = []

        for key, snapshots in self._history.items():
            k_match, k_market, k_outcome, k_book = key
            if k_match != match_id or k_market != market or k_outcome != outcome:
                continue
            if len(snapshots) < 2:
                continue

            for i in range(1, len(snapshots)):
                prev = snapshots[i - 1]
                curr = snapshots[i]
                time_diff = curr.timestamp - prev.timestamp
                odds_diff = curr.odds - prev.odds

                if time_diff <= window and abs(odds_diff) >= threshold:
                    steam_moves.append({
                        "sportsbook": k_book,
                        "from_odds": prev.odds,
                        "to_odds": curr.odds,
                        "shift": odds_diff,
                        "time_diff_seconds": time_diff,
                        "direction": "shortening" if odds_diff < 0 else "drifting",
                        "timestamp": curr.timestamp,
                    })

        return steam_moves

    def get_line_movement_summary(
        self, match_id: str, market: MarketType, outcome: Outcome
    ) -> dict:
        """Get a summary of how the line has moved across all books."""
        all_history = self.get_history(match_id, market, outcome)
        if not all_history:
            return {}

        by_book: dict[str, list[OddsSnapshot]] = defaultdict(list)
        for snap in all_history:
            by_book[snap.sportsbook].append(snap)

        summary: dict[str, dict] = {}
        for book, snaps in by_book.items():
            snaps.sort(key=lambda s: s.timestamp)
            summary[book] = {
                "opening": snaps[0].odds,
                "current": snaps[-1].odds,
                "movement": snaps[-1].odds - snaps[0].odds,
                "num_changes": len(set(s.odds for s in snaps)),
            }

        return summary

    def prune_old(self, max_age_hours: int | None = None) -> int:
        """Remove snapshots older than max_age_hours. Returns count removed."""
        max_age = max_age_hours or settings.clv_lookback_hours
        cutoff = time.time() - (max_age * 3600)
        removed = 0
        keys_to_delete = []

        for key, snapshots in self._history.items():
            original_len = len(snapshots)
            self._history[key] = [s for s in snapshots if s.timestamp >= cutoff]
            removed += original_len - len(self._history[key])
            if not self._history[key]:
                keys_to_delete.append(key)

        for key in keys_to_delete:
            del self._history[key]

        return removed

    def save(self) -> None:
        """Persist history to disk."""
        os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
        serialized = {}
        for key, snapshots in self._history.items():
            str_key = f"{key[0]}|{key[1].value}|{key[2].value}|{key[3]}"
            serialized[str_key] = [
                {
                    "match_id": s.match_id,
                    "market": s.market.value,
                    "outcome": s.outcome.value,
                    "sportsbook": s.sportsbook,
                    "odds": s.odds,
                    "line": s.line,
                    "timestamp": s.timestamp,
                }
                for s in snapshots
            ]
        with open(self._data_file, "w") as f:
            json.dump(serialized, f, indent=2)

    def load(self) -> None:
        """Load history from disk."""
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file) as f:
                serialized = json.load(f)

            for str_key, snap_dicts in serialized.items():
                parts = str_key.split("|")
                if len(parts) != 4:
                    continue
                key = (parts[0], MarketType(parts[1]), Outcome(parts[2]), parts[3])
                self._history[key] = [
                    OddsSnapshot(
                        match_id=s["match_id"],
                        market=MarketType(s["market"]),
                        outcome=Outcome(s["outcome"]),
                        sportsbook=s["sportsbook"],
                        odds=s["odds"],
                        line=s.get("line"),
                        timestamp=s["timestamp"],
                    )
                    for s in snap_dicts
                ]
            logger.info("Loaded %d odds history entries from disk", len(self._history))
        except Exception as e:
            logger.warning("Failed to load odds history: %s", e)

    @staticmethod
    def _key(match_id: str, odds_line: OddsLine) -> tuple:
        return (match_id, odds_line.market, odds_line.outcome, odds_line.sportsbook)
