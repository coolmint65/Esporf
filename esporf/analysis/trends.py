"""Trend analyzer — mines match history for statistically significant patterns.

Given a player or a head-to-head matchup, checks all relevant trend categories
and returns those that meet the minimum hit rate threshold.

Trend categories checked:
- Over/Under X.5 total goals (2.5, 3.5, 4.5, 5.5, 6.5, 7.5)
- Over/Under X.5 player goals scored
- Over/Under X.5 player goals conceded
- Both Teams to Score (BTTS) Yes/No
- Player win / draw / loss rate
- Clean sheet rate
- Player scores 2+ goals
- Player concedes 0 goals
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
        """Run all trend checks for an upcoming match and return qualifying trends."""
        trends: list[Trend] = []

        # 1. Head-to-head trends
        trends.extend(self._h2h_trends(match.home, match.away, match.league_id))

        # 2. Home player overall trends
        trends.extend(self._player_overall_trends(match.home, match.league_id))

        # 3. Away player overall trends
        trends.extend(self._player_overall_trends(match.away, match.league_id))

        # 4. Home player HOME-specific trends
        trends.extend(self._player_home_trends(match.home, match.league_id))

        # 5. Away player AWAY-specific trends
        trends.extend(self._player_away_trends(match.away, match.league_id))

        # Sort by hit rate descending, then sample size descending
        trends.sort(key=lambda t: (t.hit_rate, t.sample_size), reverse=True)

        return MatchupReport(match=match, trends=trends)

    # ── Head-to-Head Trends ──────────────────────────────────────────

    def _h2h_trends(
        self, player_a: str, player_b: str, league_id: int | None
    ) -> list[Trend]:
        """Check trends specific to when these two players face each other."""
        matches = self.db.get_h2h_matches(player_a, player_b, limit=self.last_n)
        if len(matches) < self.min_sample:
            return []

        trends: list[Trend] = []
        recent_scores = [m.score_str() for m in matches[:5]]

        # Total goals over/under
        for line in self.goal_lines:
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

        # BTTS
        btts_hits = sum(1 for m in matches if m.btts)
        trends.append(self._make_trend(
            category="BTTS - Yes",
            hits=btts_hits, total=len(matches),
            trend_type="h2h", player_a=player_a, player_b=player_b,
            league_id=league_id, recent=recent_scores,
            desc_template=f"{player_a} vs {player_b} — Both Teams to Score",
        ))
        trends.append(self._make_trend(
            category="BTTS - No",
            hits=len(matches) - btts_hits, total=len(matches),
            trend_type="h2h", player_a=player_a, player_b=player_b,
            league_id=league_id, recent=recent_scores,
            desc_template=f"{player_a} vs {player_b} — NOT Both Teams to Score",
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

        # Player A scores X+ goals in H2H
        for line in [0.5, 1.5, 2.5, 3.5]:
            hits = sum(1 for m in matches if m.goals_for(player_a) > line)
            trends.append(self._make_trend(
                category=f"{player_a} Over {line} Goals",
                hits=hits, total=len(matches),
                trend_type="h2h", player_a=player_a, player_b=player_b,
                league_id=league_id, recent=recent_scores,
                desc_template=f"{player_a} scores Over {line} vs {player_b}",
            ))

        for line in [0.5, 1.5, 2.5, 3.5]:
            hits = sum(1 for m in matches if m.goals_for(player_b) > line)
            trends.append(self._make_trend(
                category=f"{player_b} Over {line} Goals",
                hits=hits, total=len(matches),
                trend_type="h2h", player_a=player_b, player_b=player_a,
                league_id=league_id, recent=recent_scores,
                desc_template=f"{player_b} scores Over {line} vs {player_a}",
            ))

        return [t for t in trends if t is not None]

    # ── Player Overall Trends ────────────────────────────────────────

    def _player_overall_trends(
        self, player: str, league_id: int | None
    ) -> list[Trend]:
        """Check trends for a player across ALL their recent matches."""
        matches = self.db.get_player_matches(player, limit=self.last_n, league_id=league_id)
        if len(matches) < self.min_sample:
            return []

        return self._player_trend_checks(matches, player, "player_overall", league_id)

    def _player_home_trends(
        self, player: str, league_id: int | None
    ) -> list[Trend]:
        """Check trends specific to when this player plays at HOME."""
        matches = self.db.get_player_home_matches(player, limit=self.last_n, league_id=league_id)
        if len(matches) < self.min_sample:
            return []

        return self._player_trend_checks(matches, player, "player_home", league_id)

    def _player_away_trends(
        self, player: str, league_id: int | None
    ) -> list[Trend]:
        """Check trends specific to when this player plays AWAY."""
        matches = self.db.get_player_away_matches(player, limit=self.last_n, league_id=league_id)
        if len(matches) < self.min_sample:
            return []

        return self._player_trend_checks(matches, player, "player_away", league_id)

    def _player_trend_checks(
        self,
        matches: list[MatchResult],
        player: str,
        trend_type: str,
        league_id: int | None,
    ) -> list[Trend]:
        """Run all trend checks on a list of matches for one player."""
        trends: list[Trend] = []
        recent = [m.score_str() for m in matches[:5]]
        ctx = {"home": "at home", "away": "away", "overall": "overall"}
        suffix = ctx.get(trend_type.replace("player_", ""), "")

        # Total goals over/under
        for line in self.goal_lines:
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

        # Player goals scored over/under
        for line in [0.5, 1.5, 2.5, 3.5]:
            hits = sum(1 for m in matches if m.goals_for(player) > line)
            trends.append(self._make_trend(
                category=f"Player Over {line} Scored",
                hits=hits, total=len(matches),
                trend_type=trend_type, player_a=player,
                league_id=league_id, recent=recent,
                desc_template=f"{player} {suffix} — Scores over {line} goals",
            ))

        # Player goals conceded over/under
        for line in [0.5, 1.5, 2.5, 3.5]:
            hits = sum(1 for m in matches if m.goals_against(player) > line)
            trends.append(self._make_trend(
                category=f"Player Over {line} Conceded",
                hits=hits, total=len(matches),
                trend_type=trend_type, player_a=player,
                league_id=league_id, recent=recent,
                desc_template=f"{player} {suffix} — Concedes over {line} goals",
            ))

        # BTTS
        btts_hits = sum(1 for m in matches if m.btts)
        trends.append(self._make_trend(
            category="BTTS - Yes",
            hits=btts_hits, total=len(matches),
            trend_type=trend_type, player_a=player,
            league_id=league_id, recent=recent,
            desc_template=f"{player} {suffix} — BTTS Yes",
        ))
        trends.append(self._make_trend(
            category="BTTS - No",
            hits=len(matches) - btts_hits, total=len(matches),
            trend_type=trend_type, player_a=player,
            league_id=league_id, recent=recent,
            desc_template=f"{player} {suffix} — BTTS No",
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

        # Clean sheet (concedes 0)
        cs = sum(1 for m in matches if m.goals_against(player) == 0)
        trends.append(self._make_trend(
            category="Clean Sheet",
            hits=cs, total=len(matches),
            trend_type=trend_type, player_a=player,
            league_id=league_id, recent=recent,
            desc_template=f"{player} {suffix} — Clean sheet",
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
