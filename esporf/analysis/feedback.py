"""Feedback analyzer — learns from resolved picks to improve future predictions.

After each scan cycle, this module reviews all resolved picks and computes
penalty/bonus adjustments across several dimensions:

- **Market** — e.g. "Over 5.5 Goals" losing at 30% → penalize that line
- **Player** — picks involving a specific player keep losing → penalize
- **League** — one league underperforms overall → raise the bar
- **Market type** — all "Over" picks losing vs "Under" → shift preference
- **Edge bucket** — low-edge picks (12-15%) losing disproportionately → tighten

Adjustments are stored in SQLite and applied as multipliers during pick scoring.
A penalty of 0.7 means "reduce confidence by 30%". A bonus of 1.1 means
"boost confidence by 10%". Neutral is 1.0.

Minimum sample sizes prevent overreacting to small datasets.
Adjustments decay toward 1.0 as new data comes in (recency-weighted).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from esporf.models import extract_handle

logger = logging.getLogger(__name__)

# Minimum resolved picks in a dimension before we apply adjustments.
# Low threshold (3) so the engine reacts fast to losing streaks —
# waiting for 5 losses means you're already down 5+ units.
MIN_PICKS_FOR_ADJUSTMENT = 3

# Rolling window: only consider picks from the last N days
FEEDBACK_WINDOW_DAYS = 14

# How many seconds in the window
FEEDBACK_WINDOW_SECS = FEEDBACK_WINDOW_DAYS * 86400

# Dimension definitions: (name, key_extractor)
# Each extractor returns a list of keys for a given pick row.
_DIMENSIONS: list[tuple[str, Callable]] = [
    ("market",      lambda r: [r["market"]]),
    ("player",      lambda r: [extract_handle(r[f]).lower() for f in ("home", "away")]),
    ("league",      lambda r: [str(r["league_id"])]),
    ("market_type", lambda r: [_classify_market(r["market"])]),
    ("edge_bucket", lambda r: [_edge_bucket(r["edge"] or 0.0)]),
]

_FEEDBACK_COLUMNS = (
    "dimension", "dimension_value", "penalty", "win_rate",
    "sample_size", "wins", "losses", "profit", "reason", "updated_at",
)


@dataclass
class FeedbackAdjustment:
    """A learned adjustment for a specific dimension."""

    dimension: str       # "market", "player", "league", "market_type", "edge_bucket"
    value: str           # e.g. "Over 5.5 Goals", "ALPHA", "42648", "over", "low"
    penalty: float       # multiplier: <1.0 = penalize, >1.0 = boost, 1.0 = neutral
    win_rate: float      # actual win rate in this dimension
    sample_size: int     # number of resolved picks
    wins: int
    losses: int
    profit: float        # total profit in units
    reason: str          # human-readable explanation
    updated_at: int = 0


class FeedbackAnalyzer:
    """Analyzes resolved pick history to compute confidence adjustments.

    Called after resolve_pending_picks() each scan cycle. Results are cached
    and passed into the scoring engine so best_bet() can apply penalties
    to markets/players/leagues that have been underperforming.
    """

    def __init__(self, db):
        self.db = db
        self._cache: dict[str, FeedbackAdjustment] = {}

    def compute_adjustments(self) -> dict[str, FeedbackAdjustment]:
        """Recompute all feedback adjustments from recent resolved picks.

        Returns a dict keyed by "dimension:value" (e.g. "market:Over 5.5 Goals").
        """
        cutoff = int(time.time()) - FEEDBACK_WINDOW_SECS

        conn = self.db._get_conn()
        rows = conn.execute(
            """SELECT market, home, away, league_id, edge, odds, units,
                      result, profit, hit_rate
               FROM picks
               WHERE result IN ('win', 'loss')
                 AND resolved_at >= ?
               ORDER BY resolved_at DESC""",
            (cutoff,),
        ).fetchall()

        if not rows:
            self._cache = {}
            return self._cache

        # Group picks by each dimension using data-driven config
        grouped: dict[str, dict[str, list]] = {dim: {} for dim, _ in _DIMENSIONS}

        for r in rows:
            for dim_name, extractor in _DIMENSIONS:
                for key in extractor(r):
                    grouped[dim_name].setdefault(key, []).append(r)

        # Compute adjustment for each group
        adjustments: dict[str, FeedbackAdjustment] = {}
        for dim_name, groups in grouped.items():
            for key, picks in groups.items():
                adj = self._compute_one(dim_name, key, picks)
                if adj:
                    adjustments[f"{dim_name}:{key}"] = adj

        self._cache = adjustments
        self._persist(adjustments)
        self._log_summary(adjustments)

        return adjustments

    def get_penalty(
        self,
        market: str,
        home: str,
        away: str,
        league_id: int,
        edge: float,
    ) -> float:
        """Get the combined penalty multiplier for a prospective pick.

        Multiplies together all applicable dimension penalties.
        Returns a value between ~0.4 and ~1.2. Applied to the pick's
        confidence score in best_bet().
        """
        if not self._cache:
            return 1.0

        combined = 1.0

        # Market-specific penalty
        key = f"market:{market}"
        if key in self._cache:
            combined *= self._cache[key].penalty

        # Player penalties (both sides)
        for player in (home, away):
            handle = extract_handle(player).lower()
            key = f"player:{handle}"
            if key in self._cache:
                combined *= self._cache[key].penalty

        # League penalty
        key = f"league:{league_id}"
        if key in self._cache:
            combined *= self._cache[key].penalty

        # Market type penalty
        mtype = _classify_market(market)
        key = f"market_type:{mtype}"
        if key in self._cache:
            combined *= self._cache[key].penalty

        # Edge bucket penalty
        bucket = _edge_bucket(edge)
        key = f"edge_bucket:{bucket}"
        if key in self._cache:
            combined *= self._cache[key].penalty

        # Clamp to reasonable range.  Floor at 0.25 so stacking penalties
        # across multiple losing dimensions can effectively kill a pick
        # (confidence drops below the minimum threshold in best_bet).
        return max(0.25, min(1.20, combined))

    @property
    def adjustments(self) -> dict[str, FeedbackAdjustment]:
        return self._cache

    @property
    def has_data(self) -> bool:
        return len(self._cache) > 0

    def _compute_one(
        self, dimension: str, value: str, picks: list,
    ) -> FeedbackAdjustment | None:
        """Compute a single adjustment for one dimension+value group."""
        wins = sum(1 for r in picks if r["result"] == "win")
        losses = sum(1 for r in picks if r["result"] == "loss")
        total = wins + losses

        if total < MIN_PICKS_FOR_ADJUSTMENT:
            return None

        win_rate = wins / total
        profit = sum(r["profit"] for r in picks)

        penalty = _compute_penalty(win_rate, total, profit)

        reason = _build_reason(dimension, value, wins, losses, win_rate, profit, penalty)

        return FeedbackAdjustment(
            dimension=dimension,
            value=value,
            penalty=penalty,
            win_rate=win_rate,
            sample_size=total,
            wins=wins,
            losses=losses,
            profit=profit,
            reason=reason,
            updated_at=int(time.time()),
        )

    def _persist(self, adjustments: dict[str, FeedbackAdjustment]) -> None:
        """Store adjustments in the database for persistence across restarts."""
        conn = self.db._get_conn()

        conn.execute("DELETE FROM feedback_adjustments")
        if adjustments:
            conn.executemany(
                """INSERT INTO feedback_adjustments
                   (dimension, dimension_value, penalty, win_rate, sample_size,
                    wins, losses, profit, reason, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (a.dimension, a.value, a.penalty, a.win_rate,
                     a.sample_size, a.wins, a.losses, a.profit,
                     a.reason, a.updated_at)
                    for a in adjustments.values()
                ],
            )
        conn.commit()

    def load_from_db(self) -> None:
        """Load persisted adjustments on startup (before first scan completes)."""
        conn = self.db._get_conn()
        try:
            rows = conn.execute(
                """SELECT dimension, dimension_value, penalty, win_rate,
                          sample_size, wins, losses, profit, reason, updated_at
                   FROM feedback_adjustments"""
            ).fetchall()
        except Exception:
            logger.debug("feedback_adjustments table not ready yet", exc_info=True)
            return

        for r in rows:
            adj = FeedbackAdjustment(
                dimension=r["dimension"],
                value=r["dimension_value"],
                penalty=r["penalty"],
                win_rate=r["win_rate"],
                sample_size=r["sample_size"],
                wins=r["wins"],
                losses=r["losses"],
                profit=r["profit"],
                reason=r["reason"],
                updated_at=r["updated_at"],
            )
            self._cache[f"{adj.dimension}:{adj.value}"] = adj

        if self._cache:
            logger.info(
                "Loaded %d feedback adjustment(s) from DB", len(self._cache),
            )

    def _log_summary(self, adjustments: dict[str, FeedbackAdjustment]) -> None:
        """Log a summary of active adjustments for observability."""
        penalties = [a for a in adjustments.values() if a.penalty < 0.95]
        boosts = [a for a in adjustments.values() if a.penalty > 1.05]

        if penalties:
            logger.info(
                "Feedback penalties (%d): %s",
                len(penalties),
                ", ".join(
                    f"{a.dimension}:{a.value}={a.penalty:.2f} "
                    f"({a.wins}W/{a.losses}L)"
                    for a in sorted(penalties, key=lambda a: a.penalty)[:10]
                ),
            )
        if boosts:
            logger.info(
                "Feedback boosts (%d): %s",
                len(boosts),
                ", ".join(
                    f"{a.dimension}:{a.value}={a.penalty:.2f} "
                    f"({a.wins}W/{a.losses}L)"
                    for a in sorted(boosts, key=lambda a: -a.penalty)[:10]
                ),
            )
        if not penalties and not boosts:
            logger.info("Feedback: all dimensions within normal range")


def _compute_penalty(win_rate: float, sample_size: int, profit: float) -> float:
    """Compute a penalty/bonus multiplier from win rate and sample size.

    The penalty curve — neutral zone starts at 52% (breakeven for -110
    juice), not 45%.  Anything below breakeven is losing money and
    should be penalized:

    - win_rate >= 60% and profitable → bonus up to 1.15
    - win_rate 52-60% → neutral zone (1.0)
    - win_rate 40-52% → mild penalty (0.80-0.95)
    - win_rate 30-40% → moderate penalty (0.65-0.80)
    - win_rate < 30% → heavy penalty (0.45-0.65)

    Larger samples make the penalty more aggressive (higher confidence
    in the signal). Small samples (3-5 picks) are dampened toward 1.0.
    """
    # Confidence factor: larger samples get stronger adjustments
    # 3 picks → 0.5 strength, 8 → 0.75, 18+ → 1.0
    confidence = min(1.0, (sample_size - MIN_PICKS_FOR_ADJUSTMENT) / 15 + 0.5)

    if win_rate >= 0.60 and profit > 0:
        # Winning dimension — slight boost
        raw_bonus = 1.0 + (win_rate - 0.55) * 0.30
        return 1.0 + (min(raw_bonus, 1.15) - 1.0) * confidence

    if win_rate >= 0.52:
        # Neutral zone — at or above breakeven for standard juice
        return 1.0

    if win_rate >= 0.40:
        # Mild penalty — below breakeven but not terrible
        raw = 0.80 + (win_rate - 0.40) * 1.25  # 0.80 at 40%, 0.95 at 52%
        return 1.0 - (1.0 - raw) * confidence

    if win_rate >= 0.30:
        # Moderate penalty — clearly losing
        raw = 0.65 + (win_rate - 0.30) * 1.5  # 0.65 at 30%, 0.80 at 40%
        return 1.0 - (1.0 - raw) * confidence

    # Heavy penalty — this dimension is a dumpster fire
    raw = max(0.45, win_rate * 1.5)  # floors at 0.45
    return 1.0 - (1.0 - raw) * confidence


def _classify_market(market: str) -> str:
    """Classify a market string into a broad type."""
    m = market.lower()
    if m.startswith("over"):
        return "over"
    if m.startswith("under"):
        return "under"
    if "draw" in m:
        return "draw"
    if "-0.5" in m or "+0.5" in m:
        return "spread"
    if "win" in m:
        return "win"
    return "other"


def _edge_bucket(edge: float) -> str:
    """Classify an edge value into a bucket."""
    if edge < 0.18:
        return "low"      # 12-18%
    if edge < 0.25:
        return "mid"      # 18-25%
    return "high"          # 25%+


def _build_reason(
    dimension: str,
    value: str,
    wins: int,
    losses: int,
    win_rate: float,
    profit: float,
    penalty: float,
) -> str:
    """Build a human-readable reason for an adjustment."""
    total = wins + losses
    direction = "penalty" if penalty < 1.0 else "boost" if penalty > 1.0 else "neutral"
    profit_str = f"+{profit:.1f}u" if profit >= 0 else f"{profit:.1f}u"

    labels = {
        "market": f"market '{value}'",
        "player": f"player '{value}'",
        "league": f"league {value}",
        "market_type": f"'{value}' markets",
        "edge_bucket": f"'{value}' edge picks",
    }
    label = labels.get(dimension, f"{dimension}:{value}")

    return (
        f"{direction.title()} on {label}: "
        f"{wins}W/{losses}L ({win_rate:.0%}) over {total} picks, "
        f"{profit_str} profit → {penalty:.2f}x"
    )
