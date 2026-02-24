"""Tests for core data models."""

from esporf.models import League, MatchResult, MatchupReport, Trend, UpcomingMatch


class TestMatchResult:
    def _make(self, home_score=3, away_score=1) -> MatchResult:
        return MatchResult(
            match_id="123", league_id=42649, home="Player A", away="Player B",
            home_score=home_score, away_score=away_score, start_time=1700000000,
        )

    def test_total_goals(self):
        assert self._make(3, 2).total_goals == 5

    def test_winner_home(self):
        assert self._make(3, 1).winner == "Player A"

    def test_winner_away(self):
        assert self._make(1, 3).winner == "Player B"

    def test_winner_draw(self):
        assert self._make(2, 2).winner is None

    def test_is_draw(self):
        assert self._make(2, 2).is_draw is True
        assert self._make(3, 1).is_draw is False

    def test_btts(self):
        assert self._make(3, 1).btts is True
        assert self._make(3, 0).btts is False
        assert self._make(0, 0).btts is False

    def test_goals_for(self):
        m = self._make(3, 1)
        assert m.goals_for("Player A") == 3
        assert m.goals_for("Player B") == 1
        assert m.goals_for("Unknown") == 0

    def test_goals_against(self):
        m = self._make(3, 1)
        assert m.goals_against("Player A") == 1
        assert m.goals_against("Player B") == 3

    def test_won_by(self):
        m = self._make(3, 1)
        assert m.won_by("Player A") is True
        assert m.won_by("Player B") is False

    def test_score_str(self):
        assert self._make(3, 1).score_str() == "3-1"

    def test_league(self):
        assert self._make().league == League.GT_LEAGUES_12MIN


class TestUpcomingMatch:
    def test_display_name(self):
        m = UpcomingMatch(
            match_id="1", league_id=42648, home="X", away="Y", start_time=0
        )
        assert m.display_name == "X vs Y"
        assert m.league == League.ESOCCER_BATTLE_8MIN


class TestTrend:
    def test_hit_rate_pct(self):
        t = Trend(
            category="Over 5.5", description="test", hits=15, sample_size=20,
            hit_rate=0.75, trend_type="h2h", player_a="A",
        )
        assert t.hit_rate_pct == "75%"
        assert t.record == "15/20"


class TestMatchupReport:
    def test_has_trends(self):
        match = UpcomingMatch(
            match_id="1", league_id=42648, home="A", away="B", start_time=0
        )
        assert MatchupReport(match=match, trends=[]).has_trends is False

        trend = Trend(
            category="test", description="test", hits=8, sample_size=10,
            hit_rate=0.8, trend_type="h2h", player_a="A",
        )
        assert MatchupReport(match=match, trends=[trend]).has_trends is True


class TestLeague:
    def test_display_names(self):
        assert League.GT_LEAGUES_12MIN.display_name == "GT Leagues"
        assert League.ESOCCER_BATTLE_8MIN.display_name == "GG League"
        assert League.VOLTA_6MIN.display_name == "Volta"
