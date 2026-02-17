"""Edge detection engine that finds sharp betting opportunities.

Strategies implemented:
1. Line discrepancy - Find outlier odds across sportsbooks
2. Steam moves - Detect rapid line movements indicating sharp action
3. Closing line value (CLV) - Track how odds move toward close
4. Stats-based model - Use historical stats to estimate fair value
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from esporf.analysis.odds_math import (
    expected_value,
    kelly_criterion,
    remove_vig,
)
from esporf.config import settings
from esporf.models import (
    Edge,
    MarketType,
    Match,
    OddsLine,
    OddsSnapshot,
    Outcome,
)
from esporf.sources.aggregator import EnrichedMatch

logger = logging.getLogger(__name__)


class EdgeDetector:
    """Finds edges and sharp opportunities across eSoccer matches."""

    def __init__(self):
        # Historical odds snapshots for CLV and steam move detection
        self._odds_history: dict[str, list[OddsSnapshot]] = defaultdict(list)
        self._sharp_books = {"Pinnacle", "Bet365"}

    def analyze_match(self, enriched: EnrichedMatch) -> list[Edge]:
        """Run all edge detection strategies on a single match."""
        edges: list[Edge] = []

        match = enriched.match
        if not match.odds:
            return edges

        # Snapshot current odds for future CLV tracking
        self._record_snapshot(match)

        # Strategy 1: Line discrepancy across books
        edges.extend(self._find_line_discrepancies(match))

        # Strategy 2: Steam moves (rapid line movement)
        edges.extend(self._detect_steam_moves(match))

        # Strategy 3: CLV analysis
        edges.extend(self._analyze_clv(match))

        # Strategy 4: Stats-based model edges
        if enriched.home_stats and enriched.away_stats:
            edges.extend(self._stats_model_edges(enriched))

        # Filter by minimum edge threshold
        edges = [e for e in edges if e.edge_percent >= settings.min_edge_percent]

        return edges

    def analyze_all(self, matches: list[EnrichedMatch]) -> list[Edge]:
        """Analyze all matches and return sorted edges."""
        all_edges: list[Edge] = []
        for m in matches:
            all_edges.extend(self.analyze_match(m))

        # Sort by edge size descending
        all_edges.sort(key=lambda e: e.edge_percent, reverse=True)
        return all_edges

    # ── Strategy 1: Line Discrepancy ─────────────────────────────────

    def _find_line_discrepancies(self, match: Match) -> list[Edge]:
        """Find outcomes where one book's odds are significantly higher
        than the consensus, indicating a possible mispricing.

        The "fair value" is estimated by removing vig from the sharpest
        available book (Pinnacle > Bet365 > consensus average).
        """
        edges: list[Edge] = []

        for market in MarketType:
            outcomes_for_market = self._get_outcomes_for_market(market)

            for outcome in outcomes_for_market:
                book_odds = match.odds_by_sportsbook(market, outcome)
                if len(book_odds) < 2:
                    continue

                # Get fair odds from sharpest available book
                fair_odds = self._estimate_fair_odds(match, market, outcome)
                if fair_odds is None:
                    continue

                fair_prob = 1.0 / fair_odds

                # Check each book for edges vs fair value
                for book_name, odds_line in book_odds.items():
                    ev = expected_value(odds_line.odds, fair_prob)
                    if ev >= settings.min_edge_percent:
                        kelly = kelly_criterion(odds_line.odds, fair_prob)
                        edges.append(
                            Edge(
                                match=match,
                                edge_type="line_discrepancy",
                                market=market,
                                outcome=outcome,
                                best_book=book_name,
                                best_odds=odds_line.odds,
                                fair_odds=fair_odds,
                                edge_percent=ev,
                                line=odds_line.line,
                                details=(
                                    f"Edge at {book_name}: {odds_line.odds:.2f} vs "
                                    f"fair {fair_odds:.2f} | "
                                    f"EV: {ev:.1f}% | "
                                    f"Kelly: {kelly:.1%}"
                                ),
                            )
                        )

        return edges

    def _estimate_fair_odds(
        self, match: Match, market: MarketType, target_outcome: Outcome
    ) -> float | None:
        """Estimate fair odds by devigging the sharpest available book.

        Priority: Pinnacle > Bet365 > average of all books.
        """
        # Try sharp books first
        for sharp_book in self._sharp_books:
            sharp_lines = [
                o
                for o in match.odds
                if o.sportsbook == sharp_book and o.market == market
            ]
            if len(sharp_lines) >= 2:
                fair = remove_vig(sharp_lines)
                if target_outcome in fair:
                    return fair[target_outcome]

        # Fall back to average across all books
        all_lines_for_market: dict[Outcome, list[float]] = defaultdict(list)
        for o in match.odds:
            if o.market == market:
                all_lines_for_market[o.outcome].append(o.odds)

        if not all_lines_for_market or target_outcome not in all_lines_for_market:
            return None

        # Use average implied probability across books, then devig
        avg_probs: dict[Outcome, float] = {}
        for out, odds_list in all_lines_for_market.items():
            avg_odds = sum(odds_list) / len(odds_list)
            avg_probs[out] = 1.0 / avg_odds

        overround = sum(avg_probs.values())
        if overround <= 0:
            return None

        fair_prob = avg_probs[target_outcome] / overround
        if fair_prob <= 0:
            return None

        return 1.0 / fair_prob

    # ── Strategy 2: Steam Moves ──────────────────────────────────────

    def _detect_steam_moves(self, match: Match) -> list[Edge]:
        """Detect rapid odds movements at sharp books (steam moves).

        A steam move is when a sharp book moves its line significantly
        in a short window, indicating informed/sharp money. Soft books
        that haven't adjusted yet present an edge.
        """
        edges: list[Edge] = []
        now = time.time()
        window = settings.steam_move_window_seconds
        threshold = settings.steam_move_threshold
        match_key = match.match_id

        history = self._odds_history.get(match_key, [])
        if not history:
            return edges

        # Find sharp book movements within the detection window
        for sharp_book in self._sharp_books:
            for market in MarketType:
                for outcome in self._get_outcomes_for_market(market):
                    # Get recent sharp book snapshots
                    recent = [
                        s
                        for s in history
                        if s.sportsbook == sharp_book
                        and s.market == market
                        and s.outcome == outcome
                        and (now - s.timestamp) <= window
                    ]
                    if len(recent) < 2:
                        continue

                    oldest = min(recent, key=lambda s: s.timestamp)
                    newest = max(recent, key=lambda s: s.timestamp)
                    move = oldest.odds - newest.odds  # positive = odds shortened

                    if abs(move) < threshold:
                        continue

                    # Sharp book moved - check if soft books still have old line
                    current_book_odds = match.odds_by_sportsbook(market, outcome)
                    for book_name, odds_line in current_book_odds.items():
                        if book_name in self._sharp_books:
                            continue

                        # If soft book's odds are still close to the old sharp line,
                        # they haven't adjusted yet — that's an edge
                        if move > 0:
                            # Sharp shortened (more likely outcome) — bet if soft
                            # book still has higher odds
                            gap = odds_line.odds - newest.odds
                            if gap >= threshold:
                                direction = "shortened"
                                fair_odds = newest.odds
                                ev = expected_value(odds_line.odds, 1.0 / fair_odds)
                                if ev >= settings.min_edge_percent:
                                    edges.append(
                                        Edge(
                                            match=match,
                                            edge_type="steam_move",
                                            market=market,
                                            outcome=outcome,
                                            best_book=book_name,
                                            best_odds=odds_line.odds,
                                            fair_odds=fair_odds,
                                            edge_percent=ev,
                                            line=odds_line.line,
                                            details=(
                                                f"Steam move: {sharp_book} {direction} "
                                                f"{oldest.odds:.2f} → {newest.odds:.2f} | "
                                                f"{book_name} still at {odds_line.odds:.2f}"
                                            ),
                                        )
                                    )
        return edges

    # ── Strategy 3: Closing Line Value ───────────────────────────────

    def _analyze_clv(self, match: Match) -> list[Edge]:
        """Analyze closing line value — how current odds compare to
        where they opened and where they're trending.

        If a line is moving one direction and a book is slow to adjust,
        you can capture CLV by betting before they update.
        """
        edges: list[Edge] = []
        match_key = match.match_id
        history = self._odds_history.get(match_key, [])
        lookback = settings.clv_lookback_hours * 3600
        now = time.time()

        if not history:
            return edges

        for market in MarketType:
            for outcome in self._get_outcomes_for_market(market):
                # Get historical trend across ALL books for this outcome
                relevant = [
                    s
                    for s in history
                    if s.market == market
                    and s.outcome == outcome
                    and (now - s.timestamp) <= lookback
                ]
                if len(relevant) < 3:
                    continue

                # Calculate average odds at open vs now
                relevant.sort(key=lambda s: s.timestamp)
                early = relevant[: len(relevant) // 3]
                late = relevant[-len(relevant) // 3 :]

                avg_early = sum(s.odds for s in early) / len(early)
                avg_late = sum(s.odds for s in late) / len(late)

                # If odds shortened significantly (market thinks outcome more likely)
                move = avg_early - avg_late
                if abs(move) < settings.min_odds_difference:
                    continue

                # Find books that are behind the curve
                current_book_odds = match.odds_by_sportsbook(market, outcome)
                for book_name, odds_line in current_book_odds.items():
                    if move > 0:
                        # Market shortened — this outcome is more likely
                        # Edge if this book still has higher odds
                        if odds_line.odds > avg_late + settings.min_odds_difference:
                            fair_odds = avg_late
                            ev = expected_value(odds_line.odds, 1.0 / fair_odds)
                            if ev >= settings.min_edge_percent:
                                edges.append(
                                    Edge(
                                        match=match,
                                        edge_type="clv",
                                        market=market,
                                        outcome=outcome,
                                        best_book=book_name,
                                        best_odds=odds_line.odds,
                                        fair_odds=fair_odds,
                                        edge_percent=ev,
                                        line=odds_line.line,
                                        details=(
                                            f"CLV edge: market moved "
                                            f"{avg_early:.2f} → {avg_late:.2f} | "
                                            f"{book_name} lagging at {odds_line.odds:.2f}"
                                        ),
                                    )
                                )

        return edges

    # ── Strategy 4: Stats-Based Model ────────────────────────────────

    def _stats_model_edges(self, enriched: EnrichedMatch) -> list[Edge]:
        """Use historical player stats to model expected outcomes
        and compare against market odds.

        This is a simple Poisson-inspired model based on average goals
        scored/conceded to estimate match outcome probabilities.
        """
        edges: list[Edge] = []
        match = enriched.match
        home = enriched.home_stats
        away = enriched.away_stats

        if not home or not away or home.matches_played < 5 or away.matches_played < 5:
            return edges

        # Estimate expected goals using attack vs defense strength
        avg_goals_per_match = (home.avg_total_goals + away.avg_total_goals) / 2
        if avg_goals_per_match == 0:
            return edges

        home_attack = home.avg_goals_scored / avg_goals_per_match
        home_defense = home.avg_goals_conceded / avg_goals_per_match
        away_attack = away.avg_goals_scored / avg_goals_per_match
        away_defense = away.avg_goals_conceded / avg_goals_per_match

        # Expected goals for this match
        exp_home_goals = home_attack * away_defense * (avg_goals_per_match / 2)
        exp_away_goals = away_attack * home_defense * (avg_goals_per_match / 2)
        exp_total = exp_home_goals + exp_away_goals

        # Simple win probability estimate using goal expectancy ratio
        total_exp = exp_home_goals + exp_away_goals
        if total_exp <= 0:
            return edges

        home_strength = exp_home_goals / total_exp
        # Rough probability split (simplified Poisson approximation)
        est_home_win = home_strength * (1 - 0.25)  # ~25% draw probability for eSoccer
        est_draw = 0.25 * (1 - abs(home_strength - 0.5) * 2)  # lower draw prob when mismatch
        est_draw = max(est_draw, 0.05)
        est_away_win = 1.0 - est_home_win - est_draw

        # Clamp probabilities
        est_home_win = max(0.05, min(0.90, est_home_win))
        est_away_win = max(0.05, min(0.90, est_away_win))
        est_draw = max(0.05, min(0.40, est_draw))

        # Normalize
        total_p = est_home_win + est_draw + est_away_win
        est_home_win /= total_p
        est_draw /= total_p
        est_away_win /= total_p

        model_probs = {
            Outcome.HOME: est_home_win,
            Outcome.DRAW: est_draw,
            Outcome.AWAY: est_away_win,
        }

        # Check moneyline odds against model
        for outcome, model_prob in model_probs.items():
            book_odds = match.odds_by_sportsbook(MarketType.MONEYLINE, outcome)
            for book_name, odds_line in book_odds.items():
                ev = expected_value(odds_line.odds, model_prob)
                if ev >= settings.min_edge_percent:
                    fair_odds = 1.0 / model_prob
                    kelly = kelly_criterion(odds_line.odds, model_prob)
                    edges.append(
                        Edge(
                            match=match,
                            edge_type="model_edge",
                            market=MarketType.MONEYLINE,
                            outcome=outcome,
                            best_book=book_name,
                            best_odds=odds_line.odds,
                            fair_odds=fair_odds,
                            edge_percent=ev,
                            details=(
                                f"Model: {home.name} xG={exp_home_goals:.1f} vs "
                                f"{away.name} xG={exp_away_goals:.1f} | "
                                f"Model prob: {model_prob:.0%} vs "
                                f"implied: {odds_line.implied_probability:.0%} | "
                                f"Kelly: {kelly:.1%}"
                            ),
                        )
                    )

        # Check totals against expected goals
        for outcome in [Outcome.OVER, Outcome.UNDER]:
            book_odds = match.odds_by_sportsbook(MarketType.TOTAL, outcome)
            for book_name, odds_line in book_odds.items():
                if odds_line.line is None:
                    continue
                # Estimate over/under probability based on expected total
                diff = exp_total - odds_line.line
                # Simple sigmoid-like estimate
                if outcome == Outcome.OVER:
                    model_prob = 0.5 + min(0.45, max(-0.45, diff * 0.15))
                else:
                    model_prob = 0.5 - min(0.45, max(-0.45, diff * 0.15))

                model_prob = max(0.05, min(0.95, model_prob))
                ev = expected_value(odds_line.odds, model_prob)
                if ev >= settings.min_edge_percent:
                    fair_odds = 1.0 / model_prob
                    edges.append(
                        Edge(
                            match=match,
                            edge_type="model_edge",
                            market=MarketType.TOTAL,
                            outcome=outcome,
                            best_book=book_name,
                            best_odds=odds_line.odds,
                            fair_odds=fair_odds,
                            edge_percent=ev,
                            line=odds_line.line,
                            details=(
                                f"Model xTotal={exp_total:.1f} vs "
                                f"line {odds_line.line} | "
                                f"Model prob: {model_prob:.0%}"
                            ),
                        )
                    )

        return edges

    # ── Helpers ───────────────────────────────────────────────────────

    def _record_snapshot(self, match: Match) -> None:
        """Record current odds as a snapshot for future CLV/steam analysis."""
        key = match.match_id
        for o in match.odds:
            self._odds_history[key].append(
                OddsSnapshot(
                    match_id=match.match_id,
                    market=o.market,
                    outcome=o.outcome,
                    sportsbook=o.sportsbook,
                    odds=o.odds,
                    line=o.line,
                    timestamp=o.timestamp,
                )
            )

        # Prune old history (keep last N hours)
        cutoff = time.time() - (settings.clv_lookback_hours * 3600)
        self._odds_history[key] = [
            s for s in self._odds_history[key] if s.timestamp >= cutoff
        ]

    @staticmethod
    def _get_outcomes_for_market(market: MarketType) -> list[Outcome]:
        if market == MarketType.MONEYLINE:
            return [Outcome.HOME, Outcome.DRAW, Outcome.AWAY]
        elif market == MarketType.TOTAL:
            return [Outcome.OVER, Outcome.UNDER]
        elif market == MarketType.SPREAD:
            return [Outcome.HOME, Outcome.AWAY]
        elif market == MarketType.BTTS:
            return [Outcome.YES, Outcome.NO]
        return []
