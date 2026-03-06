"""Core data models for match history, player stats, and trend analysis."""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from esporf.analysis.feedback import FeedbackAnalyzer

logger = logging.getLogger(__name__)

_HANDLE_RE = re.compile(r"\(([^)]+)\)\s*$")


def extract_handle(name: str) -> str:
    """Extract the player handle from a 'Team (Handle)' string.

    Returns the handle if found, otherwise the full name unchanged.
    Examples:
        'Bayer 04 (Sheva)' → 'Sheva'
        'Arsenal (Sheva)' → 'Sheva'
        'Sheva'            → 'Sheva'
    """
    m = _HANDLE_RE.search(name)
    return m.group(1) if m else name


def extract_team(name: str) -> str | None:
    """Extract the team name from a 'Team (Handle)' string.

    Returns the team portion if present, otherwise None.
    Examples:
        'Bayer 04 (Sheva)' → 'Bayer 04'
        'Arsenal (Sheva)'  → 'Arsenal'
        'Sheva'            → None
    """
    m = _HANDLE_RE.search(name)
    if m:
        return name[: m.start()].strip()
    return None


def match_by_handles(
    home: str,
    away: str,
    start_time: int,
    candidates: list,
    *,
    time_tolerance: int = 600,
    home_attr: str = "home",
    away_attr: str = "away",
    time_attr: str = "start_time",
):
    """Find the candidate whose player handles match home/away within a time window.

    Generic helper used by kambi, bwin, and fanduel to pair their events
    with our UpcomingMatch objects.  Returns the best matching candidate
    or None.
    """
    our_pair = frozenset([extract_handle(home).lower(), extract_handle(away).lower()])
    best = None
    best_delta = time_tolerance + 1

    for c in candidates:
        c_home = extract_handle(getattr(c, home_attr)).lower()
        c_away = extract_handle(getattr(c, away_attr)).lower()
        if frozenset([c_home, c_away]) != our_pair:
            continue
        delta = abs(getattr(c, time_attr) - start_time)
        if delta <= time_tolerance and delta < best_delta:
            best = c
            best_delta = delta

    return best


def _same_player(a: str, b: str) -> bool:
    """Check if two player strings refer to the same player by handle."""
    return extract_handle(a) == extract_handle(b)


def _decimal_to_american(decimal_odds: float) -> str:
    """Convert decimal odds to American format string."""
    if decimal_odds >= 2.0:
        american = (decimal_odds - 1) * 100
        return f"+{american:.0f}"
    elif decimal_odds > 1.0:
        american = -100 / (decimal_odds - 1)
        return f"{american:.0f}"
    return "N/A"


# ── Odds data structures ────────────────────────────────────────


@dataclass
class OddsLine:
    """A single Over/Under line with odds from a sportsbook."""

    line: float  # e.g. 4.5, 5.5, 8.5
    over_odds: float  # decimal odds for Over (e.g. 1.85)
    under_odds: float  # decimal odds for Under (e.g. 1.95)
    source: str = "bet365"

    @property
    def over_american(self) -> str:
        return _decimal_to_american(self.over_odds)

    @property
    def under_american(self) -> str:
        return _decimal_to_american(self.under_odds)

    @property
    def over_implied(self) -> float:
        """Implied probability of the Over hitting (no-vig)."""
        return 1.0 / self.over_odds if self.over_odds > 0 else 1.0

    @property
    def under_implied(self) -> float:
        """Implied probability of the Under hitting (no-vig)."""
        return 1.0 / self.under_odds if self.under_odds > 0 else 1.0


@dataclass
class MoneylineOdds:
    """1X2 moneyline odds for a match."""

    home_odds: float  # decimal
    draw_odds: float  # decimal
    away_odds: float  # decimal
    source: str = "bet365"

    @property
    def home_american(self) -> str:
        return _decimal_to_american(self.home_odds)

    @property
    def draw_american(self) -> str:
        return _decimal_to_american(self.draw_odds)

    @property
    def away_american(self) -> str:
        return _decimal_to_american(self.away_odds)

    @property
    def home_implied(self) -> float:
        return 1.0 / self.home_odds if self.home_odds > 0 else 1.0

    @property
    def draw_implied(self) -> float:
        return 1.0 / self.draw_odds if self.draw_odds > 0 else 1.0

    @property
    def away_implied(self) -> float:
        return 1.0 / self.away_odds if self.away_odds > 0 else 1.0


@dataclass
class SpreadLine:
    """A handicap spread line (e.g. -0.5 / +0.5) with odds.

    handicap: the line value (e.g. -0.5 or +0.5)
    home_odds: decimal odds for the home side at this handicap
    away_odds: decimal odds for the away side at this handicap
    """

    handicap: float  # -0.5 or +0.5
    home_odds: float  # decimal
    away_odds: float  # decimal
    source: str = "kambi"

    @property
    def home_american(self) -> str:
        return _decimal_to_american(self.home_odds)

    @property
    def away_american(self) -> str:
        return _decimal_to_american(self.away_odds)

    @property
    def home_implied(self) -> float:
        return 1.0 / self.home_odds if self.home_odds > 0 else 1.0

    @property
    def away_implied(self) -> float:
        return 1.0 / self.away_odds if self.away_odds > 0 else 1.0


@dataclass
class MatchOdds:
    """All available odds for a match, fetched from BetsAPI or Kambi."""

    total_lines: list[OddsLine] = field(default_factory=list)
    moneyline: MoneylineOdds | None = None
    spreads: list[SpreadLine] = field(default_factory=list)

    def get_line(self, value: float) -> OddsLine | None:
        """Find a specific O/U line by value (e.g. 4.5)."""
        for ol in self.total_lines:
            if ol.line == value:
                return ol
        return None

    def get_spread(self, handicap: float) -> SpreadLine | None:
        """Find a specific spread line by handicap (e.g. -0.5)."""
        for sl in self.spreads:
            if sl.handicap == handicap:
                return sl
        return None

    @property
    def available_lines(self) -> list[float]:
        """Sorted list of offered O/U line values."""
        return sorted(ol.line for ol in self.total_lines)

    @property
    def has_data(self) -> bool:
        return len(self.total_lines) > 0 or self.moneyline is not None or len(self.spreads) > 0


# Display names for all known league IDs (current + legacy)
_LEAGUE_DISPLAY_NAMES: dict[int, str] = {
    # 2025 season (current)
    42648: "GG League",
    42649: "GT Leagues",
    38439: "Volta",
    # Legacy IDs (may still appear in old DB records or BetsAPI responses)
    37298: "GG League",
    23114: "GT Leagues",
}


def league_display_name(league_id: int) -> str:
    """Get the display name for any league ID (current or legacy)."""
    return _LEAGUE_DISPLAY_NAMES.get(league_id, f"League {league_id}")


class PickResult(Enum):
    """Outcome of a tracked bet pick."""

    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


@dataclass
class TrackedPick:
    """A bet pick that has been alerted, tracked for W/L resolution.

    Created when a pick is sent as an alert. Updated once the match
    ends and the result is known.
    """

    match_id: str
    league_id: int
    home: str
    away: str
    start_time: int
    market: str  # "Over 5.5 Goals", "Player -0.5", etc.
    units: float
    odds: float | None  # decimal odds at time of pick
    hit_rate: float
    edge: float | None
    result: PickResult = PickResult.PENDING
    profit: float = 0.0
    home_score: int | None = None
    away_score: int | None = None
    created_at: int = 0
    resolved_at: int | None = None
    id: int | None = None  # DB primary key

    @property
    def is_resolved(self) -> bool:
        return self.result not in (PickResult.PENDING,)

    @property
    def result_emoji(self) -> str:
        return {
            PickResult.WIN: "\u2705",
            PickResult.LOSS: "\u274c",
            PickResult.PUSH: "\u2796",
            PickResult.PENDING: "\u23f3",
            PickResult.VOID: "\u26d4",
        }[self.result]

    @property
    def profit_display(self) -> str:
        if self.profit >= 0:
            return f"+{self.profit:.2f}u"
        return f"{self.profit:.2f}u"

    @property
    def score_str(self) -> str | None:
        if self.home_score is not None and self.away_score is not None:
            return f"{self.home_score}-{self.away_score}"
        return None


class League(Enum):
    """Tracked eSoccer leagues with BetsAPI league IDs (2025 season)."""

    ESOCCER_BATTLE_8MIN = 42648
    GT_LEAGUES_12MIN = 42649
    VOLTA_6MIN = 38439

    @property
    def display_name(self) -> str:
        return league_display_name(self.value)


# Leagues where sportsbooks don't offer Over/Under goal lines.
# These leagues have moneyline (1X2) and spread (-0.5 / +0.5) only.
NO_TOTALS_LEAGUES: set[int] = {
    League.GT_LEAGUES_12MIN.value,  # 42649
    23114,                          # GT Leagues (legacy ID)
}


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
        if _same_player(player, self.home):
            return self.home_score
        elif _same_player(player, self.away):
            return self.away_score
        return 0

    def goals_against(self, player: str) -> int:
        """Goals conceded by a specific player in this match."""
        if _same_player(player, self.home):
            return self.away_score
        elif _same_player(player, self.away):
            return self.home_score
        return 0

    def won_by(self, player: str) -> bool:
        if self.winner is None:
            return False
        return _same_player(player, self.winner)

    def score_str(self) -> str:
        return f"{self.home_score}-{self.away_score}"


@dataclass
class PlayerForm:
    """Aggregated player performance stats computed from match history.

    Stored in the player_form table and rebuilt whenever new results
    come in.  Used by the trend analyzer to gate picks — players whose
    recent form drops below thresholds won't generate picks.
    """

    handle: str
    league_id: int
    matches_played: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    goals_scored: int = 0
    goals_conceded: int = 0
    avg_goals_scored: float = 0.0
    avg_goals_conceded: float = 0.0
    win_rate: float = 0.0
    over_2_5_rate: float = 0.0
    over_3_5_rate: float = 0.0
    over_4_5_rate: float = 0.0
    over_5_5_rate: float = 0.0
    # Recent form — last 10 matches
    recent_matches: int = 0
    recent_wins: int = 0
    recent_losses: int = 0
    recent_draws: int = 0
    recent_goals_scored: int = 0
    recent_goals_conceded: int = 0
    recent_win_rate: float = 0.0
    recent_over_2_5_rate: float = 0.0
    last_updated: int = 0

    @property
    def avg_total_goals(self) -> float:
        return self.avg_goals_scored + self.avg_goals_conceded

    @property
    def recent_avg_goals_scored(self) -> float:
        return self.recent_goals_scored / self.recent_matches if self.recent_matches else 0.0

    @property
    def recent_avg_goals_conceded(self) -> float:
        return self.recent_goals_conceded / self.recent_matches if self.recent_matches else 0.0

    @property
    def form_trend(self) -> str:
        """Compare recent win rate to overall — rising, falling, or stable."""
        if self.recent_matches < 5 or self.matches_played < 10:
            return "insufficient"
        diff = self.recent_win_rate - self.win_rate
        if diff > 0.10:
            return "rising"
        elif diff < -0.10:
            return "falling"
        return "stable"

    @property
    def tier(self) -> PlayerTier:
        """Dynamic tier based on recent form, sample size, and trend direction.

        Tiers control which players get picks and at what confidence level.
        Players naturally move between tiers as form changes — a slumping
        elite can drop to WATCHLIST, a rising grinder can climb to ELITE.
        """
        # Not enough data — benefit of the doubt
        if self.recent_matches < 5:
            return PlayerTier.NEW

        # Hard block: sustained poor form — only block players who are
        # clearly losing (< 20% recent win rate over 10+ matches).
        if self.recent_matches >= 10 and self.recent_win_rate < 0.20:
            return PlayerTier.BLOCKED

        # Elite: strong recent form, not trending down
        if (
            self.recent_win_rate >= 0.45
            and self.recent_matches >= 8
            and self.form_trend != "falling"
        ):
            return PlayerTier.ELITE

        # Solid: good recent form
        if self.recent_win_rate >= 0.35 and self.recent_matches >= 8:
            return PlayerTier.SOLID

        # Watchlist: marginal form or thin sample
        return PlayerTier.WATCHLIST

    @property
    def form_modifier(self) -> float:
        """Confidence multiplier based on current tier and form trend.

        Applied to the final confidence score so elite-form players
        produce higher-conviction (and higher-unit) picks while
        watchlist players get scaled back.
        """
        base = self.tier.base_modifier

        # Form trend adjustment
        trend_adj = {
            "rising": 0.05,
            "stable": 0.00,
            "falling": -0.05,
            "insufficient": -0.02,
        }.get(self.form_trend, 0.0)

        return max(0.0, base + trend_adj)


class PlayerTier(Enum):
    """Dynamic player evaluation tier.

    Players move between tiers automatically as form data updates.
    Each tier has a base confidence modifier that scales pick sizing.
    """

    ELITE = "elite"          # Strong recent form — confidence boost
    SOLID = "solid"          # Good form — baseline confidence
    WATCHLIST = "watchlist"  # Marginal form — reduced confidence
    BLOCKED = "blocked"      # Poor form — no picks
    NEW = "new"              # Insufficient data — slight caution

    @property
    def base_modifier(self) -> float:
        return {
            PlayerTier.ELITE: 1.08,
            PlayerTier.SOLID: 1.00,
            PlayerTier.WATCHLIST: 0.85,
            PlayerTier.BLOCKED: 0.00,
            PlayerTier.NEW: 0.90,
        }[self]

    @property
    def label(self) -> str:
        return {
            PlayerTier.ELITE: "Elite",
            PlayerTier.SOLID: "Solid",
            PlayerTier.WATCHLIST: "Watchlist",
            PlayerTier.BLOCKED: "Blocked",
            PlayerTier.NEW: "New",
        }[self]


@dataclass
class UpcomingMatch:
    """An upcoming/live match that we want to find trends for."""

    match_id: str
    league_id: int
    home: str
    away: str
    start_time: int
    is_live: bool = False
    odds: MatchOdds | None = None

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

        Excludes live and already-started matches — by the time a Volta
        game is in-play the lines have moved and it's too late to bet.
        """
        delta = self.start_time - time.time()
        return 0 < delta <= seconds

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

    category: str  # e.g. "Over 5.5 Goals", "Win", "Player Over 1.5 Scored"
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

    market: str  # e.g. "Over 5.5 Goals", "Player -0.5"
    confidence: float  # weighted score combining hit rate + sample size + agreement
    supporting_trends: list[Trend]  # trends that back this pick
    reason: str  # human-readable explanation
    odds_line: OddsLine | None = None  # actual O/U odds if available
    moneyline: MoneylineOdds | None = None  # actual 1X2 odds if available
    spread_line: SpreadLine | None = None  # actual spread odds if available
    _spread_side: str | None = None  # "home" or "away" for spread picks
    _ml_side: str | None = None  # "home", "away", or "draw" for moneyline picks
    edge: float | None = None  # hit_rate - implied_probability (value edge)

    @property
    def confidence_pct(self) -> str:
        return f"{self.confidence:.0%}"

    @property
    def confidence_label(self) -> str:
        if self.confidence >= 0.93:
            return "Very High"
        if self.confidence >= 0.85:
            return "High"
        if self.confidence >= 0.75:
            return "Moderate"
        return "Low"

    @property
    def decimal_odds(self) -> float | None:
        """The decimal odds for this pick's market, if real odds are attached."""
        if self.spread_line:
            if self._spread_side == "home":
                return self.spread_line.home_odds
            return self.spread_line.away_odds
        if self.odds_line:
            parsed = parse_line(self.market)
            if not parsed:
                return None
            direction = parsed[0]
            if direction.lower() == "over":
                return self.odds_line.over_odds
            return self.odds_line.under_odds
        if self.moneyline and self._ml_side:
            return _ml_side_odds(self.moneyline, self._ml_side)
        return None

    @property
    def american_odds(self) -> str | None:
        """The American odds string for this pick's market."""
        if self.spread_line:
            if self._spread_side == "home":
                return self.spread_line.home_american
            return self.spread_line.away_american
        if self.odds_line:
            parsed = parse_line(self.market)
            if not parsed:
                return None
            direction = parsed[0]
            if direction.lower() == "over":
                return self.odds_line.over_american
            return self.odds_line.under_american
        if self.moneyline and self._ml_side:
            dec = _ml_side_odds(self.moneyline, self._ml_side)
            return _decimal_to_american(dec) if dec else None
        return None

    @property
    def ev_per_unit(self) -> float | None:
        """Expected value per unit wagered using real sportsbook odds.

        Formula: EV = (hit_rate × payout) - (1 - hit_rate)
        where payout = decimal_odds - 1

        Examples:
          - O4.5 @ -240 (dec 1.417), 75% rate: EV = 0.75×0.417 - 0.25 = +0.063/u
          - O3.5 @ +130 (dec 2.30),  70% rate: EV = 0.70×1.30  - 0.30 = +0.61/u
        """
        dec = self.decimal_odds
        if dec is None or dec <= 1.0:
            return None
        avg_rate = sum(t.hit_rate for t in self.supporting_trends) / len(
            self.supporting_trends
        )
        payout = dec - 1.0
        return (avg_rate * payout) - (1.0 - avg_rate)

    @property
    def ev_display(self) -> str | None:
        """Human-readable EV per unit, e.g. '+$0.61/u'."""
        ev = self.ev_per_unit
        if ev is None:
            return None
        return f"{'+' if ev >= 0 else ''}{ev:.2f}/u"

    @property
    def is_heavy_juice(self) -> bool:
        """True if odds are -150 or worse (heavy favorite, low payout)."""
        dec = self.decimal_odds
        return dec is not None and dec < 1.667

    @property
    def units(self) -> float:
        """Recommended unit size — high units are reserved for true hammers.

        Beyond the composite confidence score, 2u and 3u plays must also
        pass hard gates on edge, trend agreement, and hit rate.  This
        prevents a single strong signal from inflating unit size.
        """
        c = self.confidence
        n_trends = len(self.supporting_trends)
        avg_rate = (
            sum(t.hit_rate for t in self.supporting_trends) / n_trends
            if n_trends
            else 0.0
        )
        edge = self.edge or 0.0

        # 3u — the ultimate hammer: everything must line up
        if c >= 0.93 and edge >= 0.15 and n_trends >= 4 and avg_rate >= 0.80:
            return 3.0

        # 2u — strong conviction: high score AND solid underlying data
        if c >= 0.85 and edge >= 0.12 and n_trends >= 3 and avg_rate >= 0.78:
            return 2.0

        # Everything else is 1u — still a recommended play, just standard size
        return 1.0

    @property
    def units_display(self) -> str:
        u = self.units
        if u == int(u):
            return f"{int(u)}u"
        return f"{u}u"


@dataclass
class MatchupReport:
    """All qualifying trends for an upcoming matchup, sent as an alert."""

    match: UpcomingMatch
    trends: list[Trend]
    generated_at: float = field(default_factory=time.time)
    avg_goals: float | None = None  # match-specific expected total goals
    form_modifier: float = 1.0  # combined form quality of both players
    skip_reason: str | None = None  # set when a player is BLOCKED or skipped
    feedback_analyzer: FeedbackAnalyzer | None = None

    @property
    def has_trends(self) -> bool:
        return len(self.trends) > 0

    @cached_property
    def best_bet(self) -> BetPick | None:
        """Pick the single best bet from qualifying trends.

        Cached so repeated access (filtering, recording, alerting) returns
        the same BetPick instance without re-running the scoring algorithm.

        When real odds are available (from BetsAPI), we:
        1. Only consider lines actually offered by the sportsbook
        2. Use real implied probability instead of estimates
        3. Score by edge (hit_rate - implied_probability) for maximum value

        When no odds are available, returns None (sportsbook odds required).
        """
        if not self.trends:
            return None

        odds = self.match.odds
        has_real_odds = odds is not None and odds.has_data

        # Group trends by category (e.g. "Over 5.5 Goals")
        market_groups: dict[str, list[Trend]] = {}
        for t in self.trends:
            market_groups.setdefault(t.category, []).append(t)

        if has_real_odds:
            return self._best_bet_with_odds(market_groups, odds)

        # No real sportsbook odds — no pick
        return None

    def _best_bet_with_odds(
        self,
        market_groups: dict[str, list[Trend]],
        odds: MatchOdds,
    ) -> BetPick | None:
        """Pick the best bet using real sportsbook odds for EV-based scoring.

        Scoring is now EV-centric: we rank by expected value per unit wagered,
        not just edge. A +130 line with 20% edge is far more valuable than
        a -240 line with 10% edge.

        Minimum 8% edge required — smaller edges get eaten by vig/variance.
        At least 2 trends must agree on a market to recommend it.
        Max juice is -150 (decimal 1.667) — anything worse is rejected.
        """
        MIN_EDGE = 0.12  # 12% minimum edge to recommend (up from 8%)
        MAX_JUICE_ODDS = 1.667  # -150 American; reject anything below
        MIN_REAL_TRENDS = 3  # minimum real (non-synthetic) trends backing a pick

        best_market: str | None = None
        best_score = 0.0
        best_trends: list[Trend] = []
        best_edge = 0.0
        best_odds_line: OddsLine | None = None
        best_ml: MoneylineOdds | None = None
        best_ml_side: str | None = None
        best_spread: SpreadLine | None = None
        best_spread_side: str | None = None

        for market, trends in market_groups.items():
            parsed = parse_line(market)
            parsed_spread = parse_spread(market)
            agreement = len(trends)
            # Count only real trends (sample_size >= 5) toward agreement;
            # synthetic signals like Forebet (sample_size=1) can boost
            # confidence but shouldn't meet the threshold on their own.
            real_trends = [t for t in trends if t.sample_size >= 5]
            if len(real_trends) < MIN_REAL_TRENDS:
                continue  # need at least 3 real trends backing a pick
            # Weight avg_rate by sample size so large-sample trends
            # contribute more than small-sample or synthetic ones.
            total_weight = sum(t.sample_size for t in trends)
            avg_rate = (
                sum(t.hit_rate * t.sample_size for t in trends) / total_weight
                if total_weight > 0
                else 0.0
            )
            avg_sample = sum(t.sample_size for t in trends) / agreement

            if parsed:
                direction, line = parsed
                odds_line = odds.get_line(line)
                if not odds_line:
                    continue  # line not offered by the book — skip it

                # Reject phantom lines: Over/Under picks too far from the
                # expected total are near-guaranteed hits with garbage odds
                # (e.g. Over 2.5 when avg_goals is 7.0).  These inflate
                # P/L without representing real bettable value.
                if self.avg_goals and self.avg_goals > 0:
                    if direction.lower() == "over" and line < self.avg_goals - 3.0:
                        continue
                    if direction.lower() == "under" and line > self.avg_goals + 3.0:
                        continue

                if direction.lower() == "over":
                    implied = odds_line.over_implied
                    dec_odds = odds_line.over_odds
                else:
                    implied = odds_line.under_implied
                    dec_odds = odds_line.under_odds

                if dec_odds < MAX_JUICE_ODDS:
                    continue  # juice worse than -150 — payout too low

                edge = avg_rate - implied
                if edge < MIN_EDGE:
                    continue  # not enough edge to overcome vig/variance

                # EV per unit: (hit_rate × payout) - miss_rate
                payout = dec_odds - 1.0
                ev_per_unit = (avg_rate * payout) - (1.0 - avg_rate)

                # Score: 35% EV quality, 25% edge, 20% agreement, 20% hit rate
                # EV quality normalized: +0.50/u or more = perfect score
                ev_norm = min(max(ev_per_unit, 0.0) / 0.50, 1.0)
                edge_norm = min(edge / 0.30, 1.0)

                score = (
                    ev_norm * 0.35
                    + edge_norm * 0.25
                    + min(agreement / 5, 1.0) * 0.20
                    + avg_rate * 0.20
                )
                if score > best_score:
                    best_score = score
                    best_market = market
                    best_trends = trends
                    best_edge = edge
                    best_odds_line = odds_line
                    best_ml = None
                    best_ml_side = None
                    best_spread = None
                    best_spread_side = None

            elif parsed_spread and odds.spreads:
                # Spread market (e.g. "PlayerName -0.5") — match to spread odds
                player_name, handicap = parsed_spread
                spread = odds.get_spread(handicap)
                if not spread:
                    continue

                # Determine if player is home or away
                side = _get_spread_side(player_name, self.match)
                if side is None:
                    continue

                if side == "home":
                    implied = spread.home_implied
                    dec_odds = spread.home_odds
                else:
                    implied = spread.away_implied
                    dec_odds = spread.away_odds

                if dec_odds < MAX_JUICE_ODDS:
                    continue  # juice worse than -150

                edge = avg_rate - implied
                if edge < MIN_EDGE:
                    continue

                payout = dec_odds - 1.0
                ev_per_unit = (avg_rate * payout) - (1.0 - avg_rate)

                ev_norm = min(max(ev_per_unit, 0.0) / 0.50, 1.0)
                edge_norm = min(edge / 0.30, 1.0)

                score = (
                    ev_norm * 0.35
                    + edge_norm * 0.25
                    + min(agreement / 5, 1.0) * 0.20
                    + avg_rate * 0.20
                )
                if score > best_score:
                    best_score = score
                    best_market = market
                    best_trends = trends
                    best_edge = edge
                    best_odds_line = None
                    best_ml = None
                    best_ml_side = None
                    best_spread = spread
                    best_spread_side = side

            else:
                # Non-line market (Win, Draw) — use moneyline odds if available
                ml = odds.moneyline
                if not ml:
                    continue
                implied = _get_moneyline_implied(market, ml, self.match)
                if implied is None:
                    continue
                ml_dec = _get_moneyline_dec_odds(market, ml, self.match)
                if ml_dec is not None and ml_dec < MAX_JUICE_ODDS:
                    continue  # juice worse than -150
                edge = avg_rate - implied
                if edge < MIN_EDGE:
                    continue
                edge_norm = min(edge / 0.30, 1.0)
                score = (
                    edge_norm * 0.35
                    + avg_rate * 0.25
                    + min(agreement / 5, 1.0) * 0.20
                    + min(avg_sample / 20, 1.0) * 0.20
                )
                if score > best_score:
                    best_score = score
                    best_market = market
                    best_trends = trends
                    best_edge = edge
                    best_odds_line = None
                    best_ml = ml
                    best_ml_side = _get_moneyline_side(market, self.match)
                    best_spread = None
                    best_spread_side = None

        if best_market is None:
            return None

        sources = ", ".join(sorted({_trend_source_label(t.trend_type) for t in best_trends}))
        top_rate = max(t.hit_rate for t in best_trends)

        # Build reason with actual odds
        odds_str = ""
        if best_odds_line:
            parsed = parse_line(best_market)
            if parsed:
                direction = parsed[0]
                if direction.lower() == "over":
                    odds_str = f" @ {best_odds_line.over_american}"
                else:
                    odds_str = f" @ {best_odds_line.under_american}"
        elif best_spread:
            if best_spread_side == "home":
                odds_str = f" @ {best_spread.home_american}"
            else:
                odds_str = f" @ {best_spread.away_american}"
        elif best_ml and best_ml_side:
            ml_dec = _ml_side_odds(best_ml, best_ml_side)
            if ml_dec:
                odds_str = f" @ {_decimal_to_american(ml_dec)}"

        reason = (
            f"{best_market}{odds_str} backed by {len(best_trends)} trend(s) "
            f"({sources}) — {top_rate:.0%} hit rate, {best_edge:.0%} edge"
        )

        # Apply form modifier — elite-form players boost confidence
        # (and unit sizing), poor-form players scale it down.
        adjusted_score = best_score * self.form_modifier

        # Apply feedback penalty — learned from historical loss patterns.
        # Penalizes markets/players/leagues that have been underperforming.
        if self.feedback_analyzer is not None:
            try:
                feedback_penalty = self.feedback_analyzer.get_penalty(
                    market=best_market,
                    home=self.match.home,
                    away=self.match.away,
                    league_id=self.match.league_id,
                    edge=best_edge,
                )
                adjusted_score *= feedback_penalty
            except Exception:
                logger.debug("Feedback penalty failed for %s", best_market, exc_info=True)

        return BetPick(
            market=best_market,
            confidence=adjusted_score,
            supporting_trends=best_trends,
            reason=reason,
            odds_line=best_odds_line,
            moneyline=best_ml,
            _ml_side=best_ml_side,
            spread_line=best_spread,
            _spread_side=best_spread_side,
            edge=best_edge,
        )

def _trend_source_label(trend_type: str) -> str:
    return {
        "h2h": "H2H",
        "player_overall": "Overall",
        "player_home": "Home form",
        "player_away": "Away form",
        "tc_player": "TotalCorner",
        "forebet": "Forebet",
    }.get(trend_type, trend_type)


# ── Line-selection helpers ──────────────────────────────────────

_LINE_RE = re.compile(r"(Over|Under)\s+(\d+(?:\.\d+)?)\s+Goals$", re.IGNORECASE)
_SPREAD_RE = re.compile(r"(.+?)\s+([+-]\d+(?:\.\d+)?)$")


def parse_line(market: str) -> tuple[str, float] | None:
    """Extract direction and line value from a market name.

    Returns e.g. ("Under", 4.5) or None for non-line markets.
    """
    m = _LINE_RE.match(market)
    if not m:
        return None
    return m.group(1), float(m.group(2))


def parse_spread(market: str) -> tuple[str, float] | None:
    """Extract player name and handicap from a spread market.

    Returns e.g. ("PlayerName", -0.5) or None for non-spread markets.
    """
    m = _SPREAD_RE.match(market)
    if not m:
        return None
    return m.group(1), float(m.group(2))



def _poisson_over_prob(avg_goals: float, line: float) -> float:
    """Probability that total goals exceeds a .5 line using Poisson distribution.

    For line=5.5 and avg_goals=6.0:
        P(total > 5.5) = P(total >= 6) = 1 - P(total <= 5)
    """
    if avg_goals <= 0:
        return 0.0
    k_max = int(line)  # 5.5 → 5, so P(total <= 5)
    cdf = 0.0
    for k in range(k_max + 1):
        cdf += (avg_goals ** k) * math.exp(-avg_goals) / math.factorial(k)
    return max(0.0, min(1.0, 1.0 - cdf))


def _get_spread_side(player_name: str, match: UpcomingMatch) -> str | None:
    """Determine if a player is home or away in a match.

    Returns "home", "away", or None if the player can't be matched.
    """
    pn = player_name.lower()
    home_handle = extract_handle(match.home).lower()
    away_handle = extract_handle(match.away).lower()

    if home_handle in pn or pn in home_handle or match.home.lower() in pn:
        return "home"
    if away_handle in pn or pn in away_handle or match.away.lower() in pn:
        return "away"
    return None


def _ml_side_odds(ml: MoneylineOdds, side: str) -> float | None:
    """Get the decimal odds for a specific moneyline side."""
    if side == "home":
        return ml.home_odds if ml.home_odds > 1.0 else None
    if side == "away":
        return ml.away_odds if ml.away_odds > 1.0 else None
    if side == "draw":
        return ml.draw_odds if ml.draw_odds > 1.0 else None
    return None


def _get_moneyline_side(market: str, match: UpcomingMatch) -> str | None:
    """Determine the moneyline side (home/away/draw) from a market string."""
    market_lower = market.lower()
    if "draw" in market_lower:
        return "draw"
    if "win" in market_lower:
        home_handle = extract_handle(match.home).lower()
        away_handle = extract_handle(match.away).lower()
        if home_handle in market_lower or match.home.lower() in market_lower:
            return "home"
        if away_handle in market_lower or match.away.lower() in market_lower:
            return "away"
    return None


def _get_moneyline_implied(
    market: str, ml: MoneylineOdds, match: UpcomingMatch
) -> float | None:
    """Get the implied probability for a moneyline market using real odds."""
    market_lower = market.lower()
    if "draw" in market_lower:
        return ml.draw_implied
    if "win" in market_lower:
        for player, implied in [
            (match.home, ml.home_implied),
            (match.away, ml.away_implied),
        ]:
            if extract_handle(player).lower() in market_lower:
                return implied
            if player.lower() in market_lower:
                return implied
        # If we can't match the player, skip
        return None
    return None


def _get_moneyline_dec_odds(
    market: str, ml: MoneylineOdds, match: UpcomingMatch
) -> float | None:
    """Get the decimal odds for a moneyline market."""
    market_lower = market.lower()
    if "draw" in market_lower:
        return ml.draw_odds
    if "win" in market_lower:
        for player, dec in [
            (match.home, ml.home_odds),
            (match.away, ml.away_odds),
        ]:
            if extract_handle(player).lower() in market_lower:
                return dec
            if player.lower() in market_lower:
                return dec
    return None


