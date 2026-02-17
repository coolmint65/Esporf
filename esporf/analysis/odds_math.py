"""Odds math utilities for converting between formats and computing fair values."""

from __future__ import annotations

from esporf.models import MarketType, OddsLine, Outcome


def remove_vig(odds_lines: list[OddsLine]) -> dict[Outcome, float]:
    """Remove the vig/juice from a set of odds to estimate fair probabilities.

    Uses the multiplicative method: divide each implied probability by the
    total overround to get no-vig probabilities, then convert back to
    fair decimal odds.

    Args:
        odds_lines: All outcome odds for a single market from one sportsbook.

    Returns:
        Dict mapping each outcome to its estimated fair decimal odds.
    """
    if not odds_lines:
        return {}

    implied_probs = {o.outcome: o.implied_probability for o in odds_lines}
    overround = sum(implied_probs.values())

    if overround <= 0:
        return {}

    fair_odds: dict[Outcome, float] = {}
    for outcome, ip in implied_probs.items():
        fair_prob = ip / overround
        if fair_prob > 0:
            fair_odds[outcome] = 1.0 / fair_prob

    return fair_odds


def compute_overround(odds_lines: list[OddsLine]) -> float:
    """Compute the total overround (vig) for a set of market odds.

    Returns the overround as a percentage above 100%.
    E.g., 105.3 means 5.3% vig.
    """
    total = sum(o.implied_probability for o in odds_lines)
    return total * 100


def expected_value(your_odds: float, fair_probability: float) -> float:
    """Calculate expected value of a bet as a percentage.

    Args:
        your_odds: The decimal odds you can bet at.
        fair_probability: The estimated true probability (0-1).

    Returns:
        EV as a percentage. Positive = profitable edge.
    """
    # EV = (probability * (odds - 1)) - (1 - probability)
    # Simplified: EV = (probability * odds) - 1
    return (fair_probability * your_odds - 1) * 100


def implied_to_decimal(implied_prob: float) -> float:
    """Convert implied probability to decimal odds."""
    if implied_prob <= 0 or implied_prob >= 1:
        return 0.0
    return 1.0 / implied_prob


def kelly_criterion(odds: float, fair_probability: float, fraction: float = 0.25) -> float:
    """Calculate Kelly Criterion bet size as a fraction of bankroll.

    Uses fractional Kelly (default 1/4 Kelly) for safer sizing.

    Args:
        odds: Decimal odds available.
        fair_probability: Estimated true probability.
        fraction: Kelly fraction (0.25 = quarter Kelly).

    Returns:
        Recommended bet size as fraction of bankroll (0.0 to 1.0).
    """
    if odds <= 1.0 or fair_probability <= 0 or fair_probability >= 1:
        return 0.0

    # Full Kelly: f = (bp - q) / b
    # where b = odds - 1, p = fair_probability, q = 1 - p
    b = odds - 1
    q = 1 - fair_probability
    full_kelly = (b * fair_probability - q) / b

    if full_kelly <= 0:
        return 0.0

    return min(full_kelly * fraction, 0.10)  # cap at 10% of bankroll


def pinnacle_fair_odds(
    pin_home: float, pin_draw: float | None, pin_away: float
) -> dict[str, float]:
    """Estimate fair odds using Pinnacle lines (the sharpest book).

    Pinnacle typically runs ~2-3% vig on eSoccer, so removing their
    vig gives a good approximation of true market probabilities.
    """
    lines = [
        OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.HOME, odds=pin_home),
        OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.AWAY, odds=pin_away),
    ]
    if pin_draw is not None:
        lines.append(
            OddsLine(sportsbook="Pinnacle", market=MarketType.MONEYLINE, outcome=Outcome.DRAW, odds=pin_draw)
        )

    fair = remove_vig(lines)
    return {o.value: v for o, v in fair.items()}
