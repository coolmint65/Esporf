"""Tests for the SQLite match database."""

import os
import tempfile

import pytest

from esporf.database import MatchDatabase
from esporf.models import MatchResult


@pytest.fixture
def db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = MatchDatabase(db_path=path)
    yield database
    database.close()
    os.unlink(path)


def _make_match(
    match_id="m1", home="Player A", away="Player B",
    home_score=3, away_score=1, league_id=23114, start_time=1000,
) -> MatchResult:
    return MatchResult(
        match_id=match_id, league_id=league_id, home=home, away=away,
        home_score=home_score, away_score=away_score, start_time=start_time,
    )


class TestInsert:
    def test_insert_and_count(self, db):
        db.insert_match(_make_match())
        assert db.total_matches() == 1

    def test_insert_duplicate_ignored(self, db):
        db.insert_match(_make_match())
        db.insert_match(_make_match())
        assert db.total_matches() == 1

    def test_insert_many(self, db):
        matches = [
            _make_match(match_id="m1", start_time=1000),
            _make_match(match_id="m2", start_time=2000),
            _make_match(match_id="m3", start_time=3000),
        ]
        added = db.insert_many(matches)
        assert added == 3
        assert db.total_matches() == 3


class TestQueries:
    @pytest.fixture(autouse=True)
    def seed_data(self, db):
        """Seed the database with test matches."""
        matches = [
            _make_match("m1", "Alpha", "Bravo", 3, 1, start_time=1000),
            _make_match("m2", "Bravo", "Alpha", 2, 2, start_time=2000),
            _make_match("m3", "Alpha", "Charlie", 4, 0, start_time=3000),
            _make_match("m4", "Charlie", "Alpha", 1, 3, start_time=4000),
            _make_match("m5", "Bravo", "Charlie", 2, 1, start_time=5000),
        ]
        db.insert_many(matches)

    def test_get_player_matches(self, db):
        matches = db.get_player_matches("Alpha")
        assert len(matches) == 4  # Alpha is in m1, m2, m3, m4

    def test_get_player_matches_ordered_by_time(self, db):
        matches = db.get_player_matches("Alpha")
        times = [m.start_time for m in matches]
        assert times == sorted(times, reverse=True)

    def test_get_player_home_matches(self, db):
        matches = db.get_player_home_matches("Alpha")
        assert len(matches) == 2  # Alpha is home in m1, m3
        assert all(m.home == "Alpha" for m in matches)

    def test_get_player_away_matches(self, db):
        matches = db.get_player_away_matches("Alpha")
        assert len(matches) == 2  # Alpha is away in m2, m4
        assert all(m.away == "Alpha" for m in matches)

    def test_get_h2h_matches(self, db):
        matches = db.get_h2h_matches("Alpha", "Bravo")
        assert len(matches) == 2  # m1, m2

    def test_get_h2h_both_directions(self, db):
        assert len(db.get_h2h_matches("Alpha", "Bravo")) == 2
        assert len(db.get_h2h_matches("Bravo", "Alpha")) == 2

    def test_get_all_players(self, db):
        players = db.get_all_players()
        assert set(players) == {"Alpha", "Bravo", "Charlie"}

    def test_total_matches_for_league(self, db):
        assert db.total_matches_for_league(23114) == 5
        assert db.total_matches_for_league(99999) == 0
