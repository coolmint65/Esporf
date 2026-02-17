"""Tests for core data models."""

from esporf.models import Edge, League, MarketType, Match, OddsLine, Outcome


class TestOddsLine:
    def test_implied_probability(self):
        line = OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.0)
        assert line.implied_probability == 0.5

    def test_implied_probability_heavy_favorite(self):
        line = OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.25)
        assert abs(line.implied_probability - 0.8) < 0.001

    def test_american_odds_positive(self):
        line = OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=3.0)
        assert line.american_odds() == "+200"

    def test_american_odds_negative(self):
        line = OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.5)
        assert line.american_odds() == "-200"

    def test_american_odds_even(self):
        line = OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.0)
        assert line.american_odds() == "+100"


class TestMatch:
    def _make_match(self) -> Match:
        return Match(
            match_id="123",
            league_id=23114,
            home="Player A",
            away="Player B",
            start_time=1700000000,
            odds=[
                OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.10),
                OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.05),
                OddsLine(sportsbook="Bet365", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=3.20),
                OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=3.10),
            ],
        )

    def test_league_lookup(self):
        m = self._make_match()
        assert m.league == League.GT_LEAGUES_12MIN

    def test_display_name(self):
        m = self._make_match()
        assert m.display_name == "Player A vs Player B"

    def test_best_odds(self):
        m = self._make_match()
        best = m.best_odds(MarketType.MONEYLINE, Outcome.HOME)
        assert best is not None
        assert best.odds == 2.10
        assert best.sportsbook == "Bet365"

    def test_best_odds_not_found(self):
        m = self._make_match()
        best = m.best_odds(MarketType.TOTAL, Outcome.OVER)
        assert best is None

    def test_odds_by_sportsbook(self):
        m = self._make_match()
        books = m.odds_by_sportsbook(MarketType.MONEYLINE, Outcome.HOME)
        assert len(books) == 2
        assert books["Bet365"].odds == 2.10
        assert books["Pinnacle"].odds == 2.05


class TestLeague:
    def test_display_name(self):
        assert League.GT_LEAGUES_12MIN.display_name == "eSoccer GT Leagues (12 min)"
        assert League.GG_LEAGUE_8MIN.display_name == "eSoccer GG League (8 min)"
        assert League.VOLTA_6MIN.display_name == "eSoccer Volta (6 min)"
