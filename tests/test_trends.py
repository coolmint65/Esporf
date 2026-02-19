"""Tests for the trend analyzer."""

import os
import tempfile

import pytest

from esporf.analysis.trends import TrendAnalyzer
from esporf.database import MatchDatabase
from esporf.models import MatchResult, UpcomingMatch


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = MatchDatabase(db_path=path)
    yield database
    database.close()
    os.unlink(path)


def _seed_high_scoring_h2h(db: MatchDatabase) -> None:
    """Seed 15 H2H matches between Alpha and Bravo, all high-scoring."""
    for i in range(15):
        db.insert_match(MatchResult(
            match_id=f"h2h_{i}",
            league_id=23114,
            home="Alpha" if i % 2 == 0 else "Bravo",
            away="Bravo" if i % 2 == 0 else "Alpha",
            home_score=4,
            away_score=3,
            start_time=1000 + i * 100,
        ))


def _seed_dominant_player(db: MatchDatabase) -> None:
    """Seed 15 matches where Alpha wins most and scores a lot."""
    opponents = ["Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]
    for i in range(15):
        opp = opponents[i % len(opponents)]
        db.insert_match(MatchResult(
            match_id=f"dom_{i}",
            league_id=23114,
            home="Alpha",
            away=opp,
            home_score=4 if i < 12 else 1,  # wins 12/15
            away_score=1 if i < 12 else 3,
            start_time=1000 + i * 100,
        ))


def _seed_low_scoring_player(db: MatchDatabase) -> None:
    """Seed 15 matches where Keeper barely concedes."""
    for i in range(15):
        db.insert_match(MatchResult(
            match_id=f"low_{i}",
            league_id=23114,
            home="Keeper",
            away=f"Opp{i}",
            home_score=2,
            away_score=0 if i < 12 else 1,  # clean sheet in 12/15
            start_time=1000 + i * 100,
        ))


class TestH2HTrends:
    def test_finds_over_goals_trend(self, db):
        """15 matches all with 7 total goals should trigger Over 5.5, 6.5."""
        _seed_high_scoring_h2h(db)
        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10
        analyzer.min_hit_rate = 0.70

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Alpha", away="Bravo", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)

        h2h_trends = [t for t in report.trends if t.trend_type == "h2h"]
        categories = {t.category for t in h2h_trends}
        assert "Over 5.5 Goals" in categories
        assert "Over 6.5 Goals" in categories

    def test_h2h_draw_trend(self, db):
        """H2H where all matches are draws should surface a Draw trend."""
        for i in range(15):
            db.insert_match(MatchResult(
                match_id=f"draw_{i}", league_id=23114,
                home="Alpha", away="Bravo",
                home_score=2, away_score=2,
                start_time=1000 + i * 100,
            ))
        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10
        analyzer.min_hit_rate = 0.70

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Alpha", away="Bravo", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)

        draws = [t for t in report.trends if t.category == "Draw" and t.trend_type == "h2h"]
        assert len(draws) == 1
        assert draws[0].hit_rate == 1.0

    def test_no_trends_with_insufficient_data(self, db):
        """Only 3 H2H matches should not produce any trends."""
        for i in range(3):
            db.insert_match(MatchResult(
                match_id=f"few_{i}", league_id=23114,
                home="Alpha", away="Bravo", home_score=5, away_score=4,
                start_time=1000 + i * 100,
            ))
        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Alpha", away="Bravo", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)
        h2h = [t for t in report.trends if t.trend_type == "h2h"]
        assert len(h2h) == 0


class TestPlayerTrends:
    def test_finds_win_trend(self, db):
        """Player who wins 12/15 should show as a Win trend at 80%."""
        _seed_dominant_player(db)
        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10
        analyzer.min_hit_rate = 0.70

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Alpha", away="SomeGuy", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)

        win_trends = [
            t for t in report.trends
            if t.category == "Win" and t.player_a == "Alpha"
        ]
        assert len(win_trends) >= 1
        assert win_trends[0].hit_rate == 12 / 15

    def test_finds_under_goals_trend(self, db):
        """Low-scoring player (2-0 in 12/15) triggers Under total goals trends."""
        _seed_low_scoring_player(db)
        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10
        analyzer.min_hit_rate = 0.70

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Keeper", away="SomeGuy", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)

        # All 15 matches have total goals <= 3, so Under 3.5 should be 100%
        under35 = [
            t for t in report.trends
            if t.category == "Under 3.5 Goals" and t.player_a == "Keeper"
        ]
        assert len(under35) >= 1
        assert under35[0].hit_rate == 1.0


class TestTrendFiltering:
    def test_only_returns_above_threshold(self, db):
        """Trends below the hit rate threshold should not appear."""
        for i in range(20):
            db.insert_match(MatchResult(
                match_id=f"mixed_{i}", league_id=23114,
                home="Alpha", away="Bravo",
                home_score=4 if i < 10 else 1,
                away_score=3 if i < 10 else 0,
                start_time=1000 + i * 100,
            ))

        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10
        analyzer.min_hit_rate = 0.70

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Alpha", away="Bravo", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)

        over55 = [
            t for t in report.trends
            if t.category == "Over 5.5 Goals" and t.trend_type == "h2h"
        ]
        assert len(over55) == 0

    def test_trend_record_and_rate(self, db):
        """Verify the record and hit rate are calculated correctly."""
        _seed_high_scoring_h2h(db)
        analyzer = TrendAnalyzer(db)
        analyzer.min_sample = 10
        analyzer.min_hit_rate = 0.70

        match = UpcomingMatch(
            match_id="upcoming", league_id=23114,
            home="Alpha", away="Bravo", start_time=9999,
        )
        report = analyzer.analyze_matchup(match)

        over55 = [
            t for t in report.trends
            if t.category == "Over 5.5 Goals" and t.trend_type == "h2h"
        ]
        assert len(over55) == 1
        assert over55[0].hits == 15
        assert over55[0].sample_size == 15
        assert over55[0].hit_rate == 1.0
        assert over55[0].record == "15/15"
