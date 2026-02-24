"""Trend analyzer — mines match history for statistically significant patterns.

Given a player or a head-to-head matchup, checks all relevant trend categories
and returns those that meet the minimum hit rate threshold.

Trend categories (full-time game lines only):
- Over/Under X.5 total goals → Total Goals
- Player win / draw / loss rate → Game Result (Moneyline)

External data sources (when available):
- TotalCorner: per-player Over X.5 hit rates + avg goals (trend_type "tc_player")
- Forebet: match-level predicted totals (trend_type "forebet")
"""

from __future__ import annotations

import logging

from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import (
    NO_TOTALS_LEAGUES,
    MatchResult,
    MatchupReport,
    Trend,
    UpcomingMatch,
    extract_handle,
    _poisson_over_prob,
)
from esporf.sources.forebet import ForebetPrediction
from esporf.sources.totalcorner import LeagueStats

logger = logging.getLogger(__name__)


class TrendAnalyzer:
    """Analyzes match history to find qualifying trends for a matchup."""

    # Recent win-rate below this threshold → player is "out of form"
    # and the matchup is skipped entirely.  Requires at least 10 recent
    # matches so brand-new players aren't unfairly gated.
    MIN_RECENT_WIN_RATE = 0.25

    def __init__(self, db: MatchDatabase):
        self.db = db
        self.min_hit_rate = settings.min_hit_rate
        self.min_sample = settings.min_sample_size
        self.last_n = settings.last_n_matches
        self.goal_lines = settings.goal_line_values

    def _player_out_of_form(self, player: str, league_id: int) -> bool:
        """Check if a player's recent form is too poor to pick.

        Returns True if the player has enough history and their last-10
        win rate is below MIN_RECENT_WIN_RATE.
        """
        handle = extract_handle(player)
        form = self.db.get_player_form(handle, league_id=league_id)
        if form is None or form.recent_matches < 10:
            return False  # not enough data to judge — let them through
        return form.recent_win_rate < self.MIN_RECENT_WIN_RATE

    def analyze_matchup(
        self,
        match: UpcomingMatch,
        tc_stats: LeagueStats | None = None,
        forebet_pred: ForebetPrediction | None = None,
    ) -> MatchupReport:
        """Run all trend checks for an upcoming match and return qualifying trends.

        Line selection is match-specific:
        1. When real odds are available → only analyze lines the book offers
        2. When no odds → use default Volta book lines

        Players whose recent form (last 10 matches) has dropped sharply
        are skipped entirely — no trends, no picks.

        External data (TotalCorner, Forebet) adds independent trend signals
        when available, boosting agreement and confidence scores.
        """
        # ── Form gate: skip players in poor recent form ──
        if self._player_out_of_form(match.home, match.league_id):
            logger.info(
                "Skipping %s vs %s — %s is out of form",
                match.home, match.away, extract_handle(match.home),
            )
            return MatchupReport(match=match, trends=[], avg_goals=0.0)
        if self._player_out_of_form(match.away, match.league_id):
            logger.info(
                "Skipping %s vs %s — %s is out of form",
                match.home, match.away, extract_handle(match.away),
            )
            return MatchupReport(match=match, trends=[], avg_goals=0.0)

        avg_goals = self._compute_matchup_avg_goals(
            match.home, match.away, match.league_id, tc_stats=tc_stats
        )

        # Determine which goal lines to analyze
        if match.league_id in NO_TOTALS_LEAGUES:
            # GT Leagues — sportsbooks don't offer O/U goals, moneyline only
            check_lines = []
            logger.debug(
                "League %s is moneyline-only — skipping O/U lines for %s vs %s",
                match.league_id, match.home, match.away,
            )
        elif match.odds and match.odds.has_data:
            check_lines = sorted(match.odds.available_lines)
        else:
            check_lines = sorted(settings.volta_book_line_values)
            logger.debug(
                "No real odds for %s vs %s — using default book lines: %s",
                match.home, match.away, check_lines,
            )

        trends: list[Trend] = []

        # 1. Head-to-head trends
        trends.extend(self._h2h_trends(match.home, match.away, match.league_id, check_lines))

        # 2. Home player overall trends
        trends.extend(self._player_overall_trends(match.home, match.league_id, check_lines))

        # 3. Away player overall trends
        trends.extend(self._player_overall_trends(match.away, match.league_id, check_lines))

        # 4. Home player HOME-specific trends
        trends.extend(self._player_home_trends(match.home, match.league_id, check_lines))

        # 5. Away player AWAY-specific trends
        trends.extend(self._player_away_trends(match.away, match.league_id, check_lines))

        # 6. Spread trends (-0.5 / +0.5) when spread odds are available
        if match.odds and match.odds.spreads:
            trends.extend(self._spread_trends(match.home, match.away, match.league_id))

        # 7. TotalCorner per-player trends (external)
        if tc_stats:
            trends.extend(
                self._tc_player_trends(match.home, tc_stats, match.league_id, check_lines)
            )
            trends.extend(
                self._tc_player_trends(match.away, tc_stats, match.league_id, check_lines)
            )

        # 7. Forebet prediction cross-check (external)
        if forebet_pred:
            trends.extend(self._forebet_trends(forebet_pred, match.league_id, check_lines))

        # Sort by hit rate descending, then sample size descending
        trends.sort(key=lambda t: (t.hit_rate, t.sample_size), reverse=True)

        return MatchupReport(match=match, trends=trends, avg_goals=avg_goals)

    # ── Head-to-Head Trends ──────────────────────────────────────────

    def _h2h_trends(
        self,
        player_a: str,
        player_b: str,
        league_id: int | None,
        goal_lines: list[float] | None = None,
    ) -> list[Trend]:
        """Check trends specific to when these two players face each other."""
        matches = self.db.get_h2h_matches(player_a, player_b, limit=self.last_n)
        if len(matches) < self.min_sample:
            return []

        trends: list[Trend] = []
        recent_scores = [m.score_str() for m in matches[:5]]
        lines = goal_lines if goal_lines is not None else self.goal_lines

        # Total goals over/under
        for line in lines:
            hits = sum(1 for m in matches if m.total_goals > line)
            trends.append(self._make_trend(
                category=f"Over {line} Goals",
                hits=hits, total=len(matches),
                trend_type="h2h", player_a=player_a, player_b=player_b,
                league_id=league_id, recent=recent_scores,
                desc_template=f"{player_a} vs {player_b} — Over {line} total goals",
            ))

            under_hits = sum(1 for m in matches if m.total_goals < line)
            trends.append(self._make_trend(
                category=f"Under {line} Goals",
                hits=under_hits, total=len(matches),
                trend_type="h2h", player_a=player_a, player_b=player_b,
                league_id=league_id, recent=recent_scores,
                desc_template=f"{player_a} vs {player_b} — Under {line} total goals",
            ))

        # H2H win rate for player A
        a_wins = sum(1 for m in matches if m.won_by(player_a))
        trends.append(self._make_trend(
            category=f"{player_a} Win",
            hits=a_wins, total=len(matches),
            trend_type="h2h", player_a=player_a, player_b=player_b,
            league_id=league_id, recent=recent_scores,
            desc_template=f"{player_a} wins vs {player_b}",
        ))

        b_wins = sum(1 for m in matches if m.won_by(player_b))
        trends.append(self._make_trend(
            category=f"{player_b} Win",
            hits=b_wins, total=len(matches),
            trend_type="h2h", player_a=player_b, player_b=player_a,
            league_id=league_id, recent=recent_scores,
            desc_template=f"{player_b} wins vs {player_a}",
        ))

        # Draw rate in H2H
        draws = sum(1 for m in matches if m.is_draw)
        trends.append(self._make_trend(
            category="Draw",
            hits=draws, total=len(matches),
            trend_type="h2h", player_a=player_a, player_b=player_b,
            league_id=league_id, recent=recent_scores,
            desc_template=f"{player_a} vs {player_b} — Draw",
        ))

        return [t for t in trends if t is not None]

    # ── Player Overall Trends ────────────────────────────────────────

    def _player_overall_trends(
        self, player: str, league_id: int | None, goal_lines: list[float] | None = None
    ) -> list[Trend]:
        """Check trends for a player across ALL their recent matches."""
        matches = self.db.get_player_matches(player, limit=self.last_n, league_id=league_id)
        if len(matches) < self.min_sample:
            return []

        return self._player_trend_checks(matches, player, "player_overall", league_id, goal_lines)

    def _player_home_trends(
        self, player: str, league_id: int | None, goal_lines: list[float] | None = None
    ) -> list[Trend]:
        """Check trends specific to when this player plays at HOME."""
        matches = self.db.get_player_home_matches(player, limit=self.last_n, league_id=league_id)
        if len(matches) < self.min_sample:
            return []

        return self._player_trend_checks(matches, player, "player_home", league_id, goal_lines)

    def _player_away_trends(
        self, player: str, league_id: int | None, goal_lines: list[float] | None = None
    ) -> list[Trend]:
        """Check trends specific to when this player plays AWAY."""
        matches = self.db.get_player_away_matches(player, limit=self.last_n, league_id=league_id)
        if len(matches) < self.min_sample:
            return []

        return self._player_trend_checks(matches, player, "player_away", league_id, goal_lines)

    def _player_trend_checks(
        self,
        matches: list[MatchResult],
        player: str,
        trend_type: str,
        league_id: int | None,
        goal_lines: list[float] | None = None,
    ) -> list[Trend]:
        """Run all trend checks on a list of matches for one player."""
        trends: list[Trend] = []
        recent = [m.score_str() for m in matches[:5]]
        ctx = {"home": "at home", "away": "away", "overall": "overall"}
        suffix = ctx.get(trend_type.replace("player_", ""), "")
        lines = goal_lines if goal_lines is not None else self.goal_lines

        # Total goals over/under
        for line in lines:
            hits = sum(1 for m in matches if m.total_goals > line)
            trends.append(self._make_trend(
                category=f"Over {line} Goals",
                hits=hits, total=len(matches),
                trend_type=trend_type, player_a=player,
                league_id=league_id, recent=recent,
                desc_template=f"{player} {suffix} — Over {line} total goals",
            ))

            under_hits = sum(1 for m in matches if m.total_goals < line)
            trends.append(self._make_trend(
                category=f"Under {line} Goals",
                hits=under_hits, total=len(matches),
                trend_type=trend_type, player_a=player,
                league_id=league_id, recent=recent,
                desc_template=f"{player} {suffix} — Under {line} total goals",
            ))

        # Win rate
        wins = sum(1 for m in matches if m.won_by(player))
        trends.append(self._make_trend(
            category="Win",
            hits=wins, total=len(matches),
            trend_type=trend_type, player_a=player,
            league_id=league_id, recent=recent,
            desc_template=f"{player} {suffix} — Wins",
        ))

        return [t for t in trends if t is not None]

    # ── Spread Trends (-0.5 / +0.5) ─────────────────────────────────

    def _spread_trends(
        self, home: str, away: str, league_id: int | None
    ) -> list[Trend]:
        """Generate spread trend categories from win/draw history.

        -0.5 spread = player must win outright (hit when player wins)
        +0.5 spread = player wins or draws (hit when player doesn't lose)
        """
        trends: list[Trend] = []

        # H2H matches for spread analysis
        h2h = self.db.get_h2h_matches(home, away, limit=self.last_n)
        if len(h2h) >= self.min_sample:
            recent = [m.score_str() for m in h2h[:5]]

            # Home -0.5 (home must win)
            home_wins = sum(1 for m in h2h if m.won_by(home))
            trends.append(self._make_trend(
                category=f"{home} -0.5",
                hits=home_wins, total=len(h2h),
                trend_type="h2h", player_a=home, player_b=away,
                league_id=league_id, recent=recent,
                desc_template=f"{home} -0.5 vs {away} (wins outright)",
            ))

            # Home +0.5 (home wins or draws)
            home_no_loss = sum(1 for m in h2h if m.won_by(home) or m.is_draw)
            trends.append(self._make_trend(
                category=f"{home} +0.5",
                hits=home_no_loss, total=len(h2h),
                trend_type="h2h", player_a=home, player_b=away,
                league_id=league_id, recent=recent,
                desc_template=f"{home} +0.5 vs {away} (wins or draws)",
            ))

            # Away -0.5 (away must win)
            away_wins = sum(1 for m in h2h if m.won_by(away))
            trends.append(self._make_trend(
                category=f"{away} -0.5",
                hits=away_wins, total=len(h2h),
                trend_type="h2h", player_a=away, player_b=home,
                league_id=league_id, recent=recent,
                desc_template=f"{away} -0.5 vs {home} (wins outright)",
            ))

            # Away +0.5 (away wins or draws)
            away_no_loss = sum(1 for m in h2h if m.won_by(away) or m.is_draw)
            trends.append(self._make_trend(
                category=f"{away} +0.5",
                hits=away_no_loss, total=len(h2h),
                trend_type="h2h", player_a=away, player_b=home,
                league_id=league_id, recent=recent,
                desc_template=f"{away} +0.5 vs {home} (wins or draws)",
            ))

        # Overall player stats for spread trends
        for player, trend_type in [(home, "player_overall"), (away, "player_overall")]:
            matches = self.db.get_player_matches(player, limit=self.last_n, league_id=league_id)
            if len(matches) < self.min_sample:
                continue
            recent = [m.score_str() for m in matches[:5]]

            wins = sum(1 for m in matches if m.won_by(player))
            trends.append(self._make_trend(
                category=f"{player} -0.5",
                hits=wins, total=len(matches),
                trend_type=trend_type, player_a=player,
                league_id=league_id, recent=recent,
                desc_template=f"{player} -0.5 overall (wins outright)",
            ))

            no_loss = sum(1 for m in matches if m.won_by(player) or m.is_draw)
            trends.append(self._make_trend(
                category=f"{player} +0.5",
                hits=no_loss, total=len(matches),
                trend_type=trend_type, player_a=player,
                league_id=league_id, recent=recent,
                desc_template=f"{player} +0.5 overall (wins or draws)",
            ))

        return [t for t in trends if t is not None]

    # ── TotalCorner Trends (external) ────────────────────────────────

    def _tc_player_trends(
        self,
        player: str,
        tc_stats: LeagueStats,
        league_id: int | None,
        goal_lines: list[float],
    ) -> list[Trend]:
        """Create trend objects from TotalCorner's precomputed Over rates."""
        ps = tc_stats.lookup(player)
        if not ps or ps.matches_played < self.min_sample:
            return []

        trends: list[Trend] = []

        for line in goal_lines:
            rate = ps.over_rates.get(line)
            if rate is not None and rate >= self.min_hit_rate:
                hits = round(rate * ps.matches_played)
                trends.append(Trend(
                    category=f"Over {line} Goals",
                    description=(
                        f"{player} (TC {ps.matches_played} games) — "
                        f"Over {line} in {hits}/{ps.matches_played} ({rate:.0%})"
                    ),
                    hits=hits,
                    sample_size=ps.matches_played,
                    hit_rate=rate,
                    trend_type="tc_player",
                    player_a=player,
                    league_id=league_id,
                    recent_results=[],
                ))

            # Under = 1 - Over for .5 lines
            if rate is not None:
                under_rate = 1.0 - rate
                if under_rate >= self.min_hit_rate:
                    under_hits = round(under_rate * ps.matches_played)
                    trends.append(Trend(
                        category=f"Under {line} Goals",
                        description=(
                            f"{player} (TC {ps.matches_played} games) — "
                            f"Under {line} in {under_hits}/{ps.matches_played} ({under_rate:.0%})"
                        ),
                        hits=under_hits,
                        sample_size=ps.matches_played,
                        hit_rate=under_rate,
                        trend_type="tc_player",
                        player_a=player,
                        league_id=league_id,
                        recent_results=[],
                    ))

        # Win rate from TC
        if ps.win_rate >= self.min_hit_rate:
            trends.append(Trend(
                category="Win",
                description=(
                    f"{player} (TC {ps.matches_played} games) — "
                    f"Win rate {ps.wins}/{ps.matches_played} ({ps.win_rate:.0%})"
                ),
                hits=ps.wins,
                sample_size=ps.matches_played,
                hit_rate=ps.win_rate,
                trend_type="tc_player",
                player_a=player,
                league_id=league_id,
                recent_results=[],
            ))

        return trends

    # ── Forebet Trends (external) ────────────────────────────────────

    def _forebet_trends(
        self,
        pred: ForebetPrediction,
        league_id: int | None,
        goal_lines: list[float],
    ) -> list[Trend]:
        """Create trend signals from Forebet's predicted total goals.

        Forebet predictions serve as a cross-check. When Forebet's predicted
        total aligns with an Over/Under line, it adds one more source of
        agreement to that market.
        """
        trends: list[Trend] = []
        predicted_total = pred.avg_goals

        for line in goal_lines:
            if predicted_total > line:
                # Forebet predicts over this line
                # Use a synthetic hit rate based on how far above the line
                margin = predicted_total - line
                synthetic_rate = min(0.50 + margin * 0.10, 0.95)
                if synthetic_rate >= self.min_hit_rate:
                    trends.append(Trend(
                        category=f"Over {line} Goals",
                        description=(
                            f"Forebet predicts {predicted_total:.1f} total goals — "
                            f"Over {line}"
                        ),
                        hits=1,
                        sample_size=1,
                        hit_rate=synthetic_rate,
                        trend_type="forebet",
                        player_a=pred.home,
                        player_b=pred.away,
                        league_id=league_id,
                        recent_results=[
                            f"Pred: {pred.predicted_home_goals}-{pred.predicted_away_goals}"
                        ],
                    ))
            elif predicted_total < line:
                margin = line - predicted_total
                synthetic_rate = min(0.50 + margin * 0.10, 0.95)
                if synthetic_rate >= self.min_hit_rate:
                    trends.append(Trend(
                        category=f"Under {line} Goals",
                        description=(
                            f"Forebet predicts {predicted_total:.1f} total goals — "
                            f"Under {line}"
                        ),
                        hits=1,
                        sample_size=1,
                        hit_rate=synthetic_rate,
                        trend_type="forebet",
                        player_a=pred.home,
                        player_b=pred.away,
                        league_id=league_id,
                        recent_results=[
                            f"Pred: {pred.predicted_home_goals}-{pred.predicted_away_goals}"
                        ],
                    ))

        return trends

    # ── Match-specific statistics ────────────────────────────────────

    def _compute_matchup_avg_goals(
        self,
        home: str,
        away: str,
        league_id: int | None,
        tc_stats: LeagueStats | None = None,
    ) -> float | None:
        """Compute expected total goals for this specific matchup.

        Uses a weighted average:
        - H2H history (2x weight — most predictive of this exact matchup)
        - Home player overall (1x weight)
        - Away player overall (1x weight)
        - TotalCorner matchup estimate (1.5x weight — larger sample, per-player data)

        The TC estimate uses each player's avg goals scored and opponent's
        avg goals conceded to compute directional expected goals.
        """
        h2h = self.db.get_h2h_matches(home, away, limit=self.last_n)
        home_all = self.db.get_player_matches(home, limit=self.last_n, league_id=league_id)
        away_all = self.db.get_player_matches(away, limit=self.last_n, league_id=league_id)

        components: list[tuple[float, float]] = []  # (avg, weight)

        if h2h:
            h2h_avg = sum(m.total_goals for m in h2h) / len(h2h)
            components.append((h2h_avg, 2.0))

        if home_all:
            home_avg = sum(m.total_goals for m in home_all) / len(home_all)
            components.append((home_avg, 1.0))

        if away_all:
            away_avg = sum(m.total_goals for m in away_all) / len(away_all)
            components.append((away_avg, 1.0))

        # TotalCorner-based estimate: use per-player scoring/conceding rates
        tc_avg = None
        if tc_stats:
            home_ps = tc_stats.lookup(home)
            away_ps = tc_stats.lookup(away)
            if home_ps and away_ps:
                exp_home = (home_ps.avg_goals_scored + away_ps.avg_goals_conceded) / 2
                exp_away = (away_ps.avg_goals_scored + home_ps.avg_goals_conceded) / 2
                tc_avg = exp_home + exp_away
                components.append((tc_avg, 1.5))

        if not components:
            return None

        total_weight = sum(w for _, w in components)
        avg = sum(v * w for v, w in components) / total_weight

        logger.debug(
            "%s vs %s: avg_goals=%.1f (H2H=%s, home=%s, away=%s, TC=%s)",
            home, away, avg,
            f"{h2h_avg:.1f}" if h2h else "N/A",
            f"{home_avg:.1f}" if home_all else "N/A",
            f"{away_avg:.1f}" if away_all else "N/A",
            f"{tc_avg:.1f}" if tc_avg else "N/A",
        )
        return avg

    # ── Helpers ──────────────────────────────────────────────────────

    def _make_trend(
        self,
        category: str,
        hits: int,
        total: int,
        trend_type: str,
        player_a: str,
        league_id: int | None,
        recent: list[str],
        desc_template: str,
        player_b: str | None = None,
    ) -> Trend | None:
        """Create a Trend only if it meets the minimum hit rate threshold."""
        if total < self.min_sample:
            return None

        hit_rate = hits / total
        if hit_rate < self.min_hit_rate:
            return None

        description = f"{desc_template} in {hits}/{total} matches ({hit_rate:.0%})"

        return Trend(
            category=category,
            description=description,
            hits=hits,
            sample_size=total,
            hit_rate=hit_rate,
            trend_type=trend_type,
            player_a=player_a,
            player_b=player_b,
            league_id=league_id,
            recent_results=recent,
        )
