"""Core data models for match history, player stats, and trend analysis."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum


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


def _same_player(a: str, b: str) -> bool:
    """Check if two player strings refer to the same player by handle."""
    return extract_handle(a) == extract_handle(b)


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

        Key principle: we want the *tightest* line that still qualifies so
        the odds are actually bettable (targeting -180 or better).

        For Under markets the tightest = lowest line (Under 3.5 > Under 5.5).
        For Over  markets the tightest = highest line (Over 5.5 > Over 3.5).

        Non-line markets (BTTS, Win, etc.) are scored normally.

        Each candidate is also checked against a rough implied-probability
        ceiling so we don't recommend something that would be -300+.
        """
        if not self.trends:
            return None

        # Group trends by category (e.g. "Over 5.5 Goals")
        market_groups: dict[str, list[Trend]] = {}
        for t in self.trends:
            market_groups.setdefault(t.category, []).append(t)

        # ── Step 1: collapse Over/Under groups to their tightest line ──
        # If Under 4.5 and Under 7.5 both qualify, keep only Under 4.5.
        # If Over 3.5 and Over 2.5 both qualify, keep only Over 3.5.
        collapsed = _collapse_to_tightest_lines(market_groups)

        # ── Step 2: filter out markets whose implied probability is too
        #    high (estimated odds worse than -180 → ~64% implied).
        max_implied = 0.64
        filtered: dict[str, list[Trend]] = {}
        for market, trends in collapsed.items():
            implied = _estimate_implied_probability(market)
            if implied is not None and implied > max_implied:
                continue  # too juicy for the book, odds will be terrible
            filtered[market] = trends

        if not filtered:
            # Fall back to the collapsed set if everything was filtered
            filtered = collapsed

        # ── Step 3: score the remaining candidates ──
        best_market: str | None = None
        best_score = 0.0
        best_trends: list[Trend] = []

        for market, trends in filtered.items():
            agreement = len(trends)
            avg_rate = sum(t.hit_rate for t in trends) / agreement
            avg_sample = sum(t.sample_size for t in trends) / agreement

            # Tightness bonus: tighter lines get a bump because the odds
            # are better.  _line_tightness returns 0-1 (1 = tightest).
            tightness = _line_tightness(market)

            # Composite: 35% hit rate, 25% agreement, 15% sample, 25% tightness
            score = (
                avg_rate * 0.35
                + min(agreement / 5, 1.0) * 0.25
                + min(avg_sample / 20, 1.0) * 0.15
                + tightness * 0.25
            )
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


# ── Line-selection helpers ──────────────────────────────────────

_LINE_RE = re.compile(r"(Over|Under)\s+(\d+(?:\.\d+)?)\s+Goals", re.IGNORECASE)


def _parse_line(market: str) -> tuple[str, float] | None:
    """Extract direction and line value from a market name.

    Returns e.g. ("Under", 4.5) or None for non-line markets.
    """
    m = _LINE_RE.match(market)
    if not m:
        return None
    return m.group(1), float(m.group(2))


def _collapse_to_tightest_lines(
    groups: dict[str, list[Trend]],
) -> dict[str, list[Trend]]:
    """For Over/Under total-goals markets, keep only the tightest line.

    "Tightest" = lowest Under line or highest Over line that qualifies,
    because those correspond to the best available odds.
    """
    # Separate line-markets from non-line markets
    under_lines: dict[float, tuple[str, list[Trend]]] = {}
    over_lines: dict[float, tuple[str, list[Trend]]] = {}
    result: dict[str, list[Trend]] = {}

    for market, trends in groups.items():
        parsed = _parse_line(market)
        if parsed is None:
            result[market] = trends
            continue
        direction, line = parsed
        if direction.lower() == "under":
            under_lines[line] = (market, trends)
        else:
            over_lines[line] = (market, trends)

    # Keep only the tightest Under (lowest line value)
    if under_lines:
        tightest = min(under_lines)
        market, trends = under_lines[tightest]
        result[market] = trends

    # Keep only the tightest Over (highest line value)
    if over_lines:
        tightest = max(over_lines)
        market, trends = over_lines[tightest]
        result[market] = trends

    return result


def _estimate_implied_probability(market: str) -> float | None:
    """Rough estimate of the sportsbook implied probability for a line.

    These are approximate fair-odds for eSoccer Volta (6-min games that
    average ~5 total goals). Anything above ~64% implied means the book
    would price it at -180 or worse — not worth betting.

    Returns None for non-line markets (BTTS, Win, etc.).
    """
    parsed = _parse_line(market)
    if parsed is None:
        return None

    direction, line = parsed

    # Rough implied probabilities for typical Volta games:
    #   Under 7.5 ≈ 95%+   (-2000)  skip
    #   Under 6.5 ≈ 88%    (-700)   skip
    #   Under 5.5 ≈ 75%    (-300)   skip
    #   Under 4.5 ≈ 58%    (-140)   bettable
    #   Under 3.5 ≈ 38%    (+160)   bettable
    #   Over  2.5 ≈ 85%    (-550)   skip
    #   Over  3.5 ≈ 72%    (-250)   skip
    #   Over  4.5 ≈ 52%    (-110)   bettable
    #   Over  5.5 ≈ 30%    (+230)   bettable
    under_implied = {
        2.5: 0.22, 3.5: 0.38, 4.5: 0.58,
        5.5: 0.75, 6.5: 0.88, 7.5: 0.95,
    }
    over_implied = {
        2.5: 0.85, 3.5: 0.72, 4.5: 0.52,
        5.5: 0.30, 6.5: 0.15, 7.5: 0.05,
    }

    table = under_implied if direction.lower() == "under" else over_implied
    return table.get(line)


def _line_tightness(market: str) -> float:
    """Score 0-1 for how tight a line is (tighter = better odds = higher score).

    Non-line markets get 0.5 (neutral).
    """
    parsed = _parse_line(market)
    if parsed is None:
        return 0.5

    direction, line = parsed

    # Under: lower line = tighter.  Range 2.5-7.5 → map to 1.0-0.0
    # Over:  higher line = tighter. Range 2.5-7.5 → map to 0.0-1.0
    normalized = (line - 2.5) / 5.0  # 0.0 at 2.5, 1.0 at 7.5
    if direction.lower() == "under":
        return 1.0 - normalized  # Under 2.5 = 1.0, Under 7.5 = 0.0
    else:
        return normalized  # Over 7.5 = 1.0, Over 2.5 = 0.0
