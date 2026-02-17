"""Tests for the edge detection engine."""

from esporf.analysis.edge_detector import EdgeDetector
from esporf.models import MarketType, Match, OddsLine, Outcome
from esporf.sources.aggregator import EnrichedMatch
from esporf.sources.scrapers import PlayerStats


def _make_enriched_match(
    odds: list[OddsLine] | None = None,
    home_stats: PlayerStats | None = None,
    away_stats: PlayerStats | None = None,
) -> EnrichedMatch:
    match = Match(
        match_id="test_123",
        league_id=23114,
        home="Player A",
        away="Player B",
        start_time=1700000000,
        odds=odds or [],
    )
    return EnrichedMatch(match=match, home_stats=home_stats, away_stats=away_stats)


class TestLineDiscrepancy:
    def test_finds_edge_when_book_is_outlier(self):
        """A soft book with higher odds than the sharp consensus should be flagged."""
        odds = [
            # Pinnacle (sharp) — devigged fair odds should be ~2.0 for home
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.95),
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.40),
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=4.00),
            # Soft book with outlier home odds
            OddsLine(sportsbook="SoftBook", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.30),
            OddsLine(sportsbook="SoftBook", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.20),
            OddsLine(sportsbook="SoftBook", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=3.50),
        ]
        em = _make_enriched_match(odds=odds)
        detector = EdgeDetector()
        edges = detector.analyze_match(em)

        # Should find at least one edge (the SoftBook home line)
        home_edges = [
            e for e in edges
            if e.outcome == Outcome.HOME and e.best_book == "SoftBook"
        ]
        assert len(home_edges) >= 1
        assert home_edges[0].edge_percent > 0

    def test_no_edge_when_lines_agree(self):
        """No edge should be found when all books are tightly aligned."""
        odds = [
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.95),
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.40),
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=4.00),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.93),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.35),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=3.95),
        ]
        em = _make_enriched_match(odds=odds)
        detector = EdgeDetector()
        edges = detector.analyze_match(em)

        # With tight lines and default 3% threshold, shouldn't find edges
        assert len(edges) == 0


class TestStatsModelEdge:
    def test_model_finds_edge_with_stats(self):
        """When stats suggest a player is much better than odds imply,
        the model should flag an edge."""
        odds = [
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=3.00),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.20),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=2.30),
        ]
        # Home player is historically dominant
        home_stats = PlayerStats(
            name="Player A",
            matches_played=50,
            wins=35,
            draws=8,
            losses=7,
            goals_scored=100,
            goals_conceded=40,
            avg_goals_scored=2.0,
            avg_goals_conceded=0.8,
            win_rate=0.70,
        )
        away_stats = PlayerStats(
            name="Player B",
            matches_played=50,
            wins=15,
            draws=10,
            losses=25,
            goals_scored=50,
            goals_conceded=80,
            avg_goals_scored=1.0,
            avg_goals_conceded=1.6,
            win_rate=0.30,
        )
        em = _make_enriched_match(odds=odds, home_stats=home_stats, away_stats=away_stats)
        detector = EdgeDetector()
        edges = detector.analyze_match(em)

        # The model should see Player A's home odds of 3.00 as too high
        model_edges = [e for e in edges if e.edge_type == "model_edge"]
        assert len(model_edges) >= 1

    def test_no_model_edge_without_enough_data(self):
        """Model should not produce edges with insufficient match history."""
        odds = [
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.50),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.20),
            OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=2.80),
        ]
        # Only 3 matches — not enough
        home_stats = PlayerStats(
            name="Player A", matches_played=3, wins=2, draws=0, losses=1,
            goals_scored=5, goals_conceded=3, avg_goals_scored=1.67,
            avg_goals_conceded=1.0, win_rate=0.67,
        )
        away_stats = PlayerStats(
            name="Player B", matches_played=3, wins=1, draws=1, losses=1,
            goals_scored=4, goals_conceded=4, avg_goals_scored=1.33,
            avg_goals_conceded=1.33, win_rate=0.33,
        )
        em = _make_enriched_match(odds=odds, home_stats=home_stats, away_stats=away_stats)
        detector = EdgeDetector()
        edges = detector.analyze_match(em)

        model_edges = [e for e in edges if e.edge_type == "model_edge"]
        assert len(model_edges) == 0
