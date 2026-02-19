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

    def starts_within(self, seconds: int) -> bool:
        """Return True if the match starts within the next *seconds* seconds.

        Live matches always qualify. Past matches (already started but not
        marked live) are excluded.
        """
        if self.is_live:
            return True
        delta = self.start_time - time.time()
        return 0 <= delta <= seconds

    @property
    def minutes_until(self) -> int:
        """Minutes until kickoff (0 if already started or live)."""
        delta = self.start_time - time.time()
        return max(0, int(delta // 60))


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
class BetPick:
    """The single best bet recommendation derived from qualifying trends."""

    market: str  # e.g. "Over 5.5 Goals", "BTTS - Yes"
    confidence: float  # weighted score combining hit rate + sample size + agreement
    supporting_trends: list[Trend]  # trends that back this pick
    reason: str  # human-readable explanation

    @property
    def confidence_pct(self) -> str:
        return f"{self.confidence:.0%}"

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.90:
            return "Very High"
        if self.confidence >= 0.80:
            return "High"
        if self.confidence >= 0.75:
            return "Moderate"
        return "Low"


@dataclass
class MatchupReport:
    """All qualifying trends for an upcoming matchup, sent as an alert."""

    match: UpcomingMatch
    trends: list[Trend]
    generated_at: float = field(default_factory=time.time)

    @property
    def has_trends(self) -> bool:
        return len(self.trends) > 0

    @property
    def best_bet(self) -> BetPick | None:
        """Pick the single best bet from qualifying trends.

        Scoring: groups trends by market category, then scores each market by
        the number of agreeing trend sources, average hit rate, and total
        sample size. The market with the highest composite score wins.
        """
        if not self.trends:
            return None

        # Group trends by category (e.g. "Over 5.5 Goals")
        market_groups: dict[str, list[Trend]] = {}
        for t in self.trends:
            market_groups.setdefault(t.category, []).append(t)

        best_market: str | None = None
        best_score = 0.0
        best_trends: list[Trend] = []

        for market, trends in market_groups.items():
            # Agreement: how many independent sources back this market
            agreement = len(trends)
            avg_rate = sum(t.hit_rate for t in trends) / agreement
            avg_sample = sum(t.sample_size for t in trends) / agreement
            # Composite: 50% hit rate, 30% agreement, 20% sample depth
            score = (avg_rate * 0.50) + (min(agreement / 5, 1.0) * 0.30) + (min(avg_sample / 20, 1.0) * 0.20)
            if score > best_score:
                best_score = score
                best_market = market
                best_trends = trends

        if best_market is None:
            return None

        sources = ", ".join(sorted({_trend_source_label(t.trend_type) for t in best_trends}))
        top_rate = max(t.hit_rate for t in best_trends)
        reason = (
            f"{best_market} backed by {len(best_trends)} trend(s) "
            f"({sources}) — top hit rate {top_rate:.0%}"
        )

        return BetPick(
            market=best_market,
            confidence=best_score,
            supporting_trends=best_trends,
            reason=reason,
        )


def _trend_source_label(trend_type: str) -> str:
    return {
        "h2h": "H2H",
        "player_overall": "Overall",
        "player_home": "Home form",
        "player_away": "Away form",
    }.get(trend_type, trend_type)
