"""Core data models for matches, odds, and edges."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class League(Enum):
    """Tracked eSoccer leagues with BetsAPI league IDs."""

    GT_LEAGUES_12MIN = 23114
    GG_LEAGUE_8MIN = 37298
    VOLTA_6MIN = 38439

    @property
    def display_name(self) -> str:
        names = {
            23114: "eSoccer GT Leagues (12 min)",
            37298: "eSoccer GG League (8 min)",
            38439: "eSoccer Volta (6 min)",
        }
        return names.get(self.value, f"League {self.value}")


class MarketType(Enum):
    """Supported betting market types."""

    MONEYLINE = "moneyline"  # 1X2
    SPREAD = "spread"  # handicap
    TOTAL = "total"  # over/under
    BTTS = "btts"  # both teams to score


class Outcome(Enum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"


@dataclass
class OddsLine:
    """A single odds line from a specific sportsbook."""

    sportsbook: str
    market: MarketType
    outcome: Outcome
    odds: float  # decimal odds
    line: float | None = None  # spread/total value (e.g. -1.5, 2.5)
    timestamp: float = field(default_factory=time.time)

    @property
    def implied_probability(self) -> float:
        """Convert decimal odds to implied probability."""
        if self.odds <= 0:
            return 0.0
        return 1.0 / self.odds

    def american_odds(self) -> str:
        """Convert decimal odds to American format."""
        if self.odds >= 2.0:
            american = round((self.odds - 1) * 100)
            return f"+{american}"
        elif self.odds > 1.0:
            american = round(-100 / (self.odds - 1))
            return str(american)
        return "+100"


@dataclass
class Match:
    """An eSoccer match with metadata."""

    match_id: str
    league_id: int
    home: str
    away: str
    start_time: int  # unix timestamp
    is_live: bool = False
    home_score: int | None = None
    away_score: int | None = None
    odds: list[OddsLine] = field(default_factory=list)

    @property
    def league(self) -> League | None:
        try:
            return League(self.league_id)
        except ValueError:
            return None

    @property
    def display_name(self) -> str:
        return f"{self.home} vs {self.away}"

    def best_odds(self, market: MarketType, outcome: Outcome) -> OddsLine | None:
        """Get the best (highest) odds for a given market/outcome."""
        matching = [
            o for o in self.odds if o.market == market and o.outcome == outcome
        ]
        if not matching:
            return None
        return max(matching, key=lambda o: o.odds)

    def odds_by_sportsbook(
        self, market: MarketType, outcome: Outcome
    ) -> dict[str, OddsLine]:
        """Get odds grouped by sportsbook for a given market/outcome."""
        result: dict[str, OddsLine] = {}
        for o in self.odds:
            if o.market == market and o.outcome == outcome:
                result[o.sportsbook] = o
        return result


@dataclass
class Edge:
    """A detected betting edge / sharp opportunity."""

    match: Match
    edge_type: str  # e.g. "line_discrepancy", "steam_move", "clv", "model_edge"
    market: MarketType
    outcome: Outcome
    best_book: str
    best_odds: float
    fair_odds: float  # estimated true probability as decimal odds
    edge_percent: float  # EV edge as percentage
    line: float | None = None
    details: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def implied_prob(self) -> float:
        return 1.0 / self.best_odds if self.best_odds > 0 else 0

    @property
    def fair_prob(self) -> float:
        return 1.0 / self.fair_odds if self.fair_odds > 0 else 0


@dataclass
class OddsSnapshot:
    """A point-in-time snapshot of odds for historical tracking."""

    match_id: str
    market: MarketType
    outcome: Outcome
    sportsbook: str
    odds: float
    line: float | None
    timestamp: float = field(default_factory=time.time)
