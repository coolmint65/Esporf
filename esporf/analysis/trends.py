"""Trend analyzer — mines match history for statistically significant patterns.

Given a player or a head-to-head matchup, checks all relevant trend categories
and returns those that meet the minimum hit rate threshold.

Trend categories (full-time game lines only):
- Over/Under X.5 total goals → Total Goals
- Player win / draw / loss rate → Game Result (Moneyline)
"""

from __future__ import annotations

import logging

from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import MatchResult, MatchupReport, Trend, UpcomingMatch

logger = logging.getLogger(__name__)


class TrendAnalyzer:
    """Analyzes match history to find qualifying trends for a matchup."""

    def __init__(self, db: MatchDatabase):
        self.db = db
        self.min_hit_rate = settings.min_hit_rate
        self.min_sample = settings.min_sample_size
        self.last_n = settings.last_n_matches
        self.goal_lines = settings.goal_line_values

    def analyze_matchup(self, match: UpcomingMatch) -> MatchupReport:
        """Run all trend checks for an upcoming match and return qualifying trends.

        When the match has real odds data, we analyze the specific lines
        offered by the sportsbook (e.g. 4.5, 5.5, 8.5) in addition to our
        configured lines. This ensures we check every line the book is offering.
        """
        # Determine which goal lines to check: config lines + any offered by the book
        lines_to_check = set(self.goal_lines)
        if match.odds and match.odds.has_data:
            for offered in match.odds.available_lines:
                lines_to_check.add(offered)
        check_lines = sorted(lines_to_check)

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

        # Sort by hit rate descending, then sample size descending
        trends.sort(key=lambda t: (t.hit_rate, t.sample_size), reverse=True)

        return MatchupReport(match=match, trends=trends)

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
