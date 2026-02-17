"""Tests for odds math utilities."""

import pytest

from esporf.analysis.odds_math import (
    compute_overround,
    expected_value,
    implied_to_decimal,
    kelly_criterion,
    remove_vig,
)
from esporf.models import MarketType, OddsLine, Outcome


class TestRemoveVig:
    def test_basic_vig_removal(self):
        """Two-way market with known vig should produce fair odds."""
        lines = [
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.90),
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=1.90),
        ]
        fair = remove_vig(lines)
        assert abs(fair[Outcome.HOME] - 2.0) < 0.01
        assert abs(fair[Outcome.AWAY] - 2.0) < 0.01

    def test_three_way_vig_removal(self):
        """1X2 market should devig to sum to 100%."""
        lines = [
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.10),
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=3.40),
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=3.60),
        ]
        fair = remove_vig(lines)
        total_prob = sum(1.0 / v for v in fair.values())
        assert abs(total_prob - 1.0) < 0.001

    def test_empty_lines(self):
        assert remove_vig([]) == {}


class TestComputeOverround:
    def test_fair_market(self):
        lines = [
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=2.0),
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=2.0),
        ]
        assert abs(compute_overround(lines) - 100.0) < 0.01

    def test_juiced_market(self):
        lines = [
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=1.90),
            OddsLine(sportsbook="Test", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=1.90),
        ]
        overround = compute_overround(lines)
        assert overround > 100.0
        assert abs(overround - 105.26) < 0.1


class TestExpectedValue:
    def test_positive_ev(self):
        # Fair coin at 2.10 odds
        ev = expected_value(2.10, 0.50)
        assert ev > 0
        assert abs(ev - 5.0) < 0.1

    def test_negative_ev(self):
        # Fair coin at 1.90 odds
        ev = expected_value(1.90, 0.50)
        assert ev < 0

    def test_zero_ev(self):
        ev = expected_value(2.0, 0.50)
        assert abs(ev) < 0.01


class TestKellyCriterion:
    def test_positive_edge(self):
        # 50% fair prob at 2.20 odds should recommend a bet
        kelly = kelly_criterion(2.20, 0.50)
        assert kelly > 0

    def test_no_edge(self):
        # 50% fair prob at 1.90 odds — no edge
        kelly = kelly_criterion(1.90, 0.50)
        assert kelly == 0.0

    def test_capped_at_10_percent(self):
        # Even with massive edge, should cap at 10%
        kelly = kelly_criterion(10.0, 0.50)
        assert kelly <= 0.10

    def test_fractional_kelly(self):
        # Full kelly vs quarter kelly
        full = kelly_criterion(2.20, 0.50, fraction=1.0)
        quarter = kelly_criterion(2.20, 0.50, fraction=0.25)
        assert quarter < full


class TestImpliedToDecimal:
    def test_fifty_percent(self):
        assert abs(implied_to_decimal(0.50) - 2.0) < 0.01

    def test_boundary(self):
        assert implied_to_decimal(0.0) == 0.0
        assert implied_to_decimal(1.0) == 0.0
