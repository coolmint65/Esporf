"""Core data models for match history, player stats, and trend analysis."""

from __future__ import annotations

import math
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
class MatchOdds:
    """All available odds for a match, fetched from BetsAPI."""

    total_lines: list[OddsLine] = field(default_factory=list)
    moneyline: MoneylineOdds | None = None

    def get_line(self, value: float) -> OddsLine | None:
        """Find a specific O/U line by value (e.g. 4.5)."""
        for ol in self.total_lines:
            if ol.line == value:
                return ol
        return None

    @property
    def available_lines(self) -> list[float]:
        """Sorted list of offered O/U line values."""
        return sorted(ol.line for ol in self.total_lines)

    @property
    def has_data(self) -> bool:
        return len(self.total_lines) > 0 or self.moneyline is not None


class League(Enum):
    """Tracked eSoccer leagues with BetsAPI league IDs."""

    GT_LEAGUES_12MIN = 23114
    GG_LEAGUE_8MIN = 37298
    VOLTA_6MIN = 38439

    @property
    def display_name(self) -> str:
        names = {
            23114: "GT Leagues",
            37298: "GG League",
            38439: "Volta",
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

    market: str  # e.g. "Over 5.5 Goals", "Player Over 1.5 Scored"
    confidence: float  # weighted score combining hit rate + sample size + agreement
    supporting_trends: list[Trend]  # trends that back this pick
    reason: str  # human-readable explanation
    odds_line: OddsLine | None = None  # actual O/U odds if available
    moneyline: MoneylineOdds | None = None  # actual 1X2 odds if available
    edge: float | None = None  # hit_rate - implied_probability (value edge)

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

    @property
    def decimal_odds(self) -> float | None:
        """The decimal odds for this pick's market, if real odds are attached."""
        if not self.odds_line:
            return None
        parsed = _parse_line(self.market)
        if not parsed:
            return None
        direction = parsed[0]
        if direction.lower() == "over":
            return self.odds_line.over_odds
        return self.odds_line.under_odds

    @property
    def american_odds(self) -> str | None:
        """The American odds string for this pick's market."""
        if not self.odds_line:
            return None
        parsed = _parse_line(self.market)
        if not parsed:
            return None
        direction = parsed[0]
        if direction.lower() == "over":
            return self.odds_line.over_american
        return self.odds_line.under_american

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
        """True if odds are -200 or worse (heavy favorite, low payout)."""
        dec = self.decimal_odds
        return dec is not None and dec < 1.50

    @property
    def units(self) -> float:
        """Recommended unit size based on confidence level and odds quality.

        Conservative by default — higher units reserved for the strongest edges.
        Penalizes heavy juice (low payout) since even high hit rates produce
        tiny profit.
        """
        n_sources = len(self.supporting_trends)
        top_rate = max(t.hit_rate for t in self.supporting_trends)
        min_rate = min(t.hit_rate for t in self.supporting_trends)
        avg_sample = sum(t.sample_size for t in self.supporting_trends) / max(n_sources, 1)
        has_h2h = any(t.trend_type == "h2h" for t in self.supporting_trends)

        # Start at 1u, add bonuses for strong signals
        u = 1.0

        # Hit rate bonus
        if top_rate >= 0.95:
            u += 0.75
        elif top_rate >= 0.90:
            u += 0.50
        elif top_rate >= 0.85:
            u += 0.25

        # Multi-source agreement bonus
        if n_sources >= 4:
            u += 0.50
        elif n_sources >= 3:
            u += 0.25

        # Large sample bonus
        if avg_sample >= 18:
            u += 0.25

        # Perfect storm: H2H backs it, 4+ sources, ALL above 85%
        if has_h2h and n_sources >= 4 and min_rate >= 0.85:
            u += 0.50

        # Penalize heavy juice — cap units when payout is poor
        if self.is_heavy_juice:
            u = min(u, 1.25)

        # Boost for positive EV at plus-money odds (getting a good price)
        ev = self.ev_per_unit
        if ev is not None and ev > 0.20:
            u += 0.25

        # Snap DOWN to the nearest allowed tier (conservative)
        tiers = [1.0, 1.25, 1.5, 1.75, 2.0, 3.0]
        return max(t for t in tiers if t <= u)

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

    @property
    def has_trends(self) -> bool:
        return len(self.trends) > 0

    @property
    def best_bet(self) -> BetPick | None:
        """Pick the single best bet from qualifying trends.

        When real odds are available (from BetsAPI), we:
        1. Only consider lines actually offered by the sportsbook
        2. Use real implied probability instead of estimates
        3. Score by edge (hit_rate - implied_probability) for maximum value

        When no odds are available, falls back to the tightest-line heuristic.
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

        # No real sportsbook odds — fall through to best_trend_pick
        return None

    @property
    def best_trend_pick(self) -> BetPick | None:
        """Pick the best bet based on trends alone, without requiring odds.

        Used for early alerts when sportsbook odds aren't available yet.
        Only considers lines that Volta books typically offer (2.5, 3.5, 4.5).
        Calculates implied fair odds from the hit rate so the user can compare
        against whatever price their sportsbook shows.
        """
        if not self.trends:
            return None
        # Don't generate trend-only pick if we already have a real odds pick
        if self.best_bet is not None:
            return None

        from esporf.config import settings
        book_lines = set(settings.volta_book_line_values)

        market_groups: dict[str, list[Trend]] = {}
        for t in self.trends:
            market_groups.setdefault(t.category, []).append(t)

        return self._best_trend_only_pick(market_groups, book_lines)

    def _best_trend_only_pick(
        self,
        market_groups: dict[str, list[Trend]],
        book_lines: set[float],
    ) -> BetPick | None:
        """Pick the best trend using only historical data — no real odds needed.

        Strategy: pick the **tightest line** (highest number) that still
        meets the hit rate threshold. Low lines like O2.5 at 95% are
        useless — no sportsbook offers a reasonable price on a near-certainty.
        Tighter lines (O3.5 at 78%, O4.5 at 65%) are where real value lives
        because the book will actually offer bettable odds.

        Only considers lines the book typically offers (volta_book_lines).
        Requires at least the configured min_hit_rate to qualify.
        """
        from esporf.config import settings
        min_rate = settings.min_hit_rate

        # Collect qualifying lines, then pick the tightest one
        candidates: list[tuple[float, str, list[Trend]]] = []

        for market, trends in market_groups.items():
            parsed = _parse_line(market)
            if not parsed:
                continue

            direction, line = parsed
            if line not in book_lines:
                continue

            agreement = len(trends)
            avg_rate = sum(t.hit_rate for t in trends) / agreement

            if avg_rate < min_rate:
                continue

            # Must have multiple sources or strong sample to recommend
            if agreement < 2:
                avg_sample = sum(t.sample_size for t in trends) / agreement
                if avg_sample < 15:
                    continue

            candidates.append((line, market, trends))

        if not candidates:
            return None

        # Pick the tightest line (highest number) — that's where books
        # will offer real odds and where our edge is most exploitable
        candidates.sort(key=lambda c: c[0], reverse=True)
        best_line, best_market, best_trends = candidates[0]

        top_rate = max(t.hit_rate for t in best_trends)
        sources = ", ".join(sorted({_trend_source_label(t.trend_type) for t in best_trends}))

        # Confidence score for unit sizing (not used for trend-only, but
        # needed for the BetPick dataclass)
        agreement = len(best_trends)
        avg_rate = sum(t.hit_rate for t in best_trends) / agreement
        avg_sample = sum(t.sample_size for t in best_trends) / agreement
        confidence = (
            avg_rate * 0.40
            + min(agreement / 5, 1.0) * 0.30
            + min(avg_sample / 20, 1.0) * 0.30
        )

        reason = (
            f"{best_market} backed by {len(best_trends)} trend(s) "
            f"({sources}) — {top_rate:.0%} hit rate"
        )

        return BetPick(
            market=best_market,
            confidence=confidence,
            supporting_trends=best_trends,
            reason=reason,
            odds_line=None,
            moneyline=None,
            edge=None,
        )

    def _best_bet_with_odds(
        self,
        market_groups: dict[str, list[Trend]],
        odds: MatchOdds,
    ) -> BetPick | None:
        """Pick the best bet using real sportsbook odds for EV-based scoring.

        Scoring is now EV-centric: we rank by expected value per unit wagered,
        not just edge. A +130 line with 20% edge is far more valuable than
        a -240 line with 10% edge.

        Minimum 5% edge required — smaller edges get eaten by vig/variance.
        Heavy juice (> -200) is penalized in scoring since the payout is poor.
        """
        MIN_EDGE = 0.05  # 5% minimum edge to recommend

        best_market: str | None = None
        best_score = 0.0
        best_trends: list[Trend] = []
        best_edge = 0.0
        best_odds_line: OddsLine | None = None
        best_ml: MoneylineOdds | None = None

        for market, trends in market_groups.items():
            parsed = _parse_line(market)
            agreement = len(trends)
            avg_rate = sum(t.hit_rate for t in trends) / agreement
            avg_sample = sum(t.sample_size for t in trends) / agreement

            if parsed:
                direction, line = parsed
                odds_line = odds.get_line(line)
                if not odds_line:
                    continue  # line not offered by the book — skip it

                if direction.lower() == "over":
                    implied = odds_line.over_implied
                    dec_odds = odds_line.over_odds
                else:
                    implied = odds_line.under_implied
                    dec_odds = odds_line.under_odds

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

                # Penalize heavy juice — even with edge, payout is poor
                juice_penalty = 0.0
                if dec_odds < 1.50:  # worse than -200
                    juice_penalty = 0.15
                elif dec_odds < 1.67:  # worse than -150
                    juice_penalty = 0.05

                score = (
                    ev_norm * 0.35
                    + edge_norm * 0.25
                    + min(agreement / 5, 1.0) * 0.20
                    + avg_rate * 0.20
                    - juice_penalty
                )
                if score > best_score:
                    best_score = score
                    best_market = market
                    best_trends = trends
                    best_edge = edge
                    best_odds_line = odds_line
                    best_ml = None
            else:
                # Non-line market (Win, Draw) — use moneyline odds if available
                ml = odds.moneyline
                if not ml:
                    continue
                implied = _get_moneyline_implied(market, ml, self.match)
                if implied is None:
                    continue
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

        if best_market is None:
            return None

        sources = ", ".join(sorted({_trend_source_label(t.trend_type) for t in best_trends}))
        top_rate = max(t.hit_rate for t in best_trends)

        # Build reason with actual odds
        odds_str = ""
        if best_odds_line:
            parsed = _parse_line(best_market)
            if parsed:
                direction = parsed[0]
                if direction.lower() == "over":
                    odds_str = f" @ {best_odds_line.over_american}"
                else:
                    odds_str = f" @ {best_odds_line.under_american}"

        reason = (
            f"{best_market}{odds_str} backed by {len(best_trends)} trend(s) "
            f"({sources}) — {top_rate:.0%} hit rate, {best_edge:.0%} edge"
        )

        return BetPick(
            market=best_market,
            confidence=best_score,
            supporting_trends=best_trends,
            reason=reason,
            odds_line=best_odds_line,
            moneyline=best_ml,
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


def _parse_line(market: str) -> tuple[str, float] | None:
    """Extract direction and line value from a market name.

    Returns e.g. ("Under", 4.5) or None for non-line markets.
    """
    m = _LINE_RE.match(market)
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


