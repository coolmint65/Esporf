"""Core data models for match history, player stats, and trend analysis."""

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
            23114: "GT Leagues (12 min)",
            37298: "GG League (8 min)",
            38439: "Volta (6 min)",
        }
        return names.get(self.value, f"League {self.value}")


@dataclass
class MatchResult:
    """A completed eSoccer match with final scores."""

    match_id: str
    league_id: int
    home: str
    away: str
    home_score: int
    away_score: int
    start_time: int  # unix timestamp
    ht_home_score: int | None = None
    ht_away_score: int | None = None

    @property
    def total_goals(self) -> int:
        return self.home_score + self.away_score

    @property
    def winner(self) -> str | None:
        if self.home_score > self.away_score:
            return self.home
        elif self.away_score > self.home_score:
            return self.away
        return None  # draw

    @property
    def is_draw(self) -> bool:
        return self.home_score == self.away_score

    @property
    def btts(self) -> bool:
        """Both teams to score."""
        return self.home_score > 0 and self.away_score > 0

    @property
    def league(self) -> League | None:
        try:
            return League(self.league_id)
        except ValueError:
            return None

    def goals_for(self, player: str) -> int:
        """Goals scored by a specific player in this match."""
        if player == self.home:
            return self.home_score
        elif player == self.away:
            return self.away_score
        return 0

    def goals_against(self, player: str) -> int:
        """Goals conceded by a specific player in this match."""
        if player == self.home:
            return self.away_score
        elif player == self.away:
            return self.home_score
        return 0

    def won_by(self, player: str) -> bool:
        return self.winner == player

    def score_str(self) -> str:
        return f"{self.home_score}-{self.away_score}"


@dataclass
class UpcomingMatch:
    """An upcoming/live match that we want to find trends for."""

    match_id: str
    league_id: int
    home: str
    away: str
    start_time: int
    is_live: bool = False

    @property
    def display_name(self) -> str:
        return f"{self.home} vs {self.away}"

    @property
    def league(self) -> League | None:
        try:
            return League(self.league_id)
        except ValueError:
            return None


@dataclass
class Trend:
    """A statistical trend that meets the minimum hit rate threshold.

    Example: "Player A vs Player B - Over 5.5 goals in 15/20 matches (75%)"
    """

    category: str  # e.g. "Over 5.5 Goals", "BTTS - Yes", "Home Win"
    description: str  # human-readable full description
    hits: int  # number of times the trend hit
    sample_size: int  # total matches analyzed
    hit_rate: float  # hits / sample_size (0.0 - 1.0)
    trend_type: str  # "h2h", "player_overall", "player_home", "player_away"
    player_a: str  # primary player
    player_b: str | None = None  # opponent (for H2H trends)
    league_id: int | None = None
    recent_results: list[str] = field(default_factory=list)  # e.g. ["4-2","3-1","5-0"]

    @property
    def hit_rate_pct(self) -> str:
        return f"{self.hit_rate:.0%}"

    @property
    def record(self) -> str:
        return f"{self.hits}/{self.sample_size}"


@dataclass
class MatchupReport:
    """All qualifying trends for an upcoming matchup, sent as an alert."""

    match: UpcomingMatch
    trends: list[Trend]
    generated_at: float = field(default_factory=time.time)

    @property
    def has_trends(self) -> bool:
        return len(self.trends) > 0
