"""bwin public API client for eSoccer Volta odds.

bwin is one of the few sportsbooks that offers Volta-specific betting markets
(2x3 min format) with pre-match odds available well before kickoff — solving
the critical Volta odds timing problem that BetsAPI (which only surfaces Volta
matches ~2 min before start) cannot.

The CDS (Content Delivery Service) API is the same backend used by bwin.com,
BetMGM, and Sportingbet.  No API key or authentication required — just a
public access ID discoverable from bwin's client config endpoint.

Endpoints:
    https://www.bwin.com/cds-api/bettingoffer/fixtures
        ?x-bwin-accessid={ACCESS_ID}
        &lang=en&country=GB
        &sportIds=108
        &competitionIds={VOLTA_IDS}
        &offerMapping=Filtered
        &offerCategories=Gridable
        &fixtureCategories=Gridable

Markets parsed:
    - Total Goals Over/Under (multiple lines: 2.5, 3.5, 4.5)
    - Match Up Winner (3 way) — 1X2 moneyline
    - Goals Handicap — spreads (-0.5 / +0.5, -1 / +1, etc.)
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import httpx

from esporf.models import (
    MatchOdds,
    MoneylineOdds,
    OddsLine,
    SpreadLine,
    UpcomingMatch,
    extract_handle,
)

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────

# bwin CDS API base URL (www.bwin.com proxies to the CDS backend)
_CDS_BASE = "https://www.bwin.com/cds-api"

# Public access ID (discovered from bwin's /api/clientconfig endpoint).
# This is a base64-encoded GUID that changes infrequently.
_ACCESS_ID = "NTZiMjk3OGMtNjU5Mi00NjA5LWI2MWItZmU4MDRhN2QxZmEz"

# eSoccer sport ID on the bwin/Entain platform
_SPORT_ID = 108

# Known Volta competition IDs (discovered via sport navigation API).
# These can change when bwin rotates tournament formats.
_VOLTA_COMPETITION_IDS: list[int] = [
    103932,  # E-Soccer - Battle Volta FA Cup 2x3 Minutes
    104465,  # E-Soccer - Battle Volta International C 2x3 Minutes
]

# BetsAPI league ID for Volta (used when creating UpcomingMatch objects)
_VOLTA_LEAGUE_ID = 38439

# Cache TTL for fetched data (seconds)
_CACHE_TTL = 45

# Maximum time difference (seconds) when matching events by start time
_TIME_TOLERANCE = 600  # 10 minutes

# ── Handicap parsing ─────────────────────────────────────────────

_HANDICAP_RE = re.compile(r"\(([+-]?\d+(?:\.\d+)?)\)")


class BwinClient:
    """Async client for bwin's public CDS API — focused on Volta odds."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[_BwinFixture] = []
        self._cache_time: float = 0
        self._access_id: str = _ACCESS_ID

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Origin": "https://www.bwin.com",
                    "Referer": "https://www.bwin.com/",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Public methods ───────────────────────────────────────────

    async def get_volta_schedule(self) -> list[UpcomingMatch]:
        """Return upcoming Volta matches with odds already attached.

        bwin provides both schedule and odds in a single request, so
        every returned match has real sportsbook odds from the start.
        """
        fixtures = await self._fetch_volta_fixtures()
        matches: list[UpcomingMatch] = []

        for fix in fixtures:
            if fix.odds and fix.odds.has_data:
                matches.append(UpcomingMatch(
                    match_id=f"bwin_{fix.fixture_id}",
                    league_id=_VOLTA_LEAGUE_ID,
                    home=fix.home,
                    away=fix.away,
                    start_time=fix.start_time,
                    odds=fix.odds,
                ))

        return matches

    async def attach_odds(self, matches: list[UpcomingMatch]) -> int:
        """Fetch bwin Volta odds and attach to matching UpcomingMatch objects.

        Only attaches if the match doesn't already have odds.  Matches by
        player handles (case-insensitive) and approximate start time.

        Returns the number of matches that received bwin odds.
        """
        fixtures = await self._fetch_volta_fixtures()
        if not fixtures:
            return 0

        attached = 0
        for match in matches:
            if match.odds and match.odds.has_data:
                continue  # already has odds from another source
            if match.league_id != _VOLTA_LEAGUE_ID:
                continue  # only attach to Volta matches

            best = _find_matching_fixture(match, fixtures)
            if best and best.odds and best.odds.has_data:
                match.odds = best.odds
                attached += 1
                logger.info(
                    "bwin odds for %s: lines=%s, ML=%s, spreads=%s",
                    match.display_name,
                    best.odds.available_lines,
                    "yes" if best.odds.moneyline else "no",
                    [s.handicap for s in best.odds.spreads] if best.odds.spreads else "no",
                )

        return attached

    # ── Data fetching ──────────────────────────────────────────────

    async def _fetch_volta_fixtures(self) -> list[_BwinFixture]:
        """Fetch all Volta fixtures with odds from bwin CDS API.

        Results are cached for _CACHE_TTL seconds.
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < _CACHE_TTL:
            return self._cache

        client = await self._get_client()

        # Refresh access ID periodically (it can rotate)
        await self._refresh_access_id(client)

        comp_ids = ",".join(str(c) for c in _VOLTA_COMPETITION_IDS)
        url = f"{_CDS_BASE}/bettingoffer/fixtures"
        params = {
            "x-bwin-accessid": self._access_id,
            "lang": "en",
            "country": "GB",
            "sportIds": str(_SPORT_ID),
            "competitionIds": comp_ids,
            "offerMapping": "Filtered",
            "offerCategories": "Gridable",
            "fixtureCategories": "Gridable",
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("bwin: Volta fixtures fetch failed: %s", e)
            return self._cache  # return stale cache on error

        fixtures = _parse_fixtures(data)
        self._cache = fixtures
        self._cache_time = now

        if fixtures:
            with_odds = sum(1 for f in fixtures if f.odds and f.odds.has_data)
            logger.info(
                "bwin: fetched %d Volta fixtures (%d with odds)",
                len(fixtures), with_odds,
            )
        else:
            logger.debug("bwin: no Volta fixtures returned")

        return fixtures

    async def _refresh_access_id(self, client: httpx.AsyncClient) -> None:
        """Refresh the public access ID from bwin's client config.

        Called before each fixture fetch but only actually refreshes if the
        cached ID is more than 1 hour old.
        """
        # Only refresh every hour
        if hasattr(self, "_access_id_time") and (time.time() - self._access_id_time) < 3600:
            return

        try:
            resp = await client.get(
                "https://www.bwin.com/en/api/clientconfig",
                headers={
                    "x-bwin-sports-api": "prod",
                    "X-From-Product": "sports",
                },
            )
            resp.raise_for_status()
            config = resp.json()

            # Navigate the config to find the public access ID
            ms = config.get("msConnection", config.get("vnMsConnection", {}))
            new_id = ms.get("publicAccessId", "")
            if new_id and new_id != self._access_id:
                logger.info("bwin: refreshed access ID")
                self._access_id = new_id

            self._access_id_time = time.time()
        except Exception as e:
            logger.debug("bwin: access ID refresh failed (using cached): %s", e)
            self._access_id_time = time.time()  # don't retry immediately


# ── Internal data structures ──────────────────────────────────────


class _BwinFixture:
    """Parsed bwin fixture with odds."""

    __slots__ = ("fixture_id", "home", "away", "start_time", "competition", "odds")

    def __init__(
        self,
        fixture_id: str,
        home: str,
        away: str,
        start_time: int,
        competition: str,
        odds: MatchOdds | None,
    ) -> None:
        self.fixture_id = fixture_id
        self.home = home
        self.away = away
        self.start_time = start_time
        self.competition = competition
        self.odds = odds


# ── Response parsing ──────────────────────────────────────────────


def _parse_fixtures(data: dict) -> list[_BwinFixture]:
    """Parse the bwin CDS fixtures response into fixtures with odds."""
    raw_fixtures = data.get("fixtures", [])
    results: list[_BwinFixture] = []

    for fix in raw_fixtures:
        participants = fix.get("participants", [])
        if len(participants) < 2:
            continue

        # Find home and away teams
        home_name = ""
        away_name = ""
        for p in participants:
            ptype = p.get("properties", {}).get("type", "")
            name = p.get("name", {}).get("value", "")
            if ptype == "HomeTeam":
                home_name = name
            elif ptype == "AwayTeam":
                away_name = name

        if not home_name or not away_name:
            continue

        # Parse start time
        start_str = fix.get("startDate", "")
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            start_ts = int(dt.timestamp())
        except (ValueError, TypeError):
            continue

        fixture_id = str(fix.get("id", ""))
        competition = fix.get("competition", {}).get("name", {}).get("value", "")

        # Parse odds from optionMarkets
        odds = _parse_option_markets(fix.get("optionMarkets", []))

        results.append(_BwinFixture(
            fixture_id=fixture_id,
            home=home_name,
            away=away_name,
            start_time=start_ts,
            competition=competition,
            odds=odds,
        ))

    return results


def _parse_option_markets(markets: list[dict]) -> MatchOdds | None:
    """Parse all option markets for a single fixture into MatchOdds."""
    total_lines: list[OddsLine] = []
    moneyline: MoneylineOdds | None = None
    spreads: list[SpreadLine] = []
    seen_lines: set[float] = set()
    seen_handicaps: set[float] = set()

    for market in markets:
        name = market.get("name", {}).get("value", "")
        options = market.get("options", [])
        name_lower = name.lower()

        # ── Total Goals Over/Under ──
        if "total goals over/under" in name_lower and "team" not in name_lower:
            _parse_total_goals(options, total_lines, seen_lines)

        # ── 1X2 Moneyline ──
        elif "match up winner" in name_lower or "winner (3 way)" in name_lower:
            ml = _parse_moneyline(options)
            if ml:
                moneyline = ml

        # ── Handicap / Spread ──
        elif "goals handicap" in name_lower:
            _parse_handicap(options, spreads, seen_handicaps)

    if not total_lines and not moneyline and not spreads:
        return None

    return MatchOdds(total_lines=total_lines, moneyline=moneyline, spreads=spreads)


def _parse_total_goals(
    options: list[dict],
    total_lines: list[OddsLine],
    seen_lines: set[float],
) -> None:
    """Parse Over/Under options into OddsLine entries."""
    over_odds: dict[float, float] = {}
    under_odds: dict[float, float] = {}

    for opt in options:
        opt_name = opt.get("name", {}).get("value", "")
        price = opt.get("price", {})
        dec_odds = price.get("odds", 0)
        if not dec_odds or dec_odds <= 1.0:
            continue

        opt_types = opt.get("parameters", {}).get("optionTypes", [])

        # Extract the line value from the option name (e.g. "Over 4.5" → 4.5)
        match = re.search(r"(\d+(?:\.\d+)?)", opt_name)
        if not match:
            continue
        line_val = float(match.group(1))

        if "Over" in opt_types or "over" in opt_name.lower():
            over_odds[line_val] = dec_odds
        elif "Under" in opt_types or "under" in opt_name.lower():
            under_odds[line_val] = dec_odds

    # Pair up over/under for each line
    for line_val in sorted(set(over_odds.keys()) & set(under_odds.keys())):
        if round(line_val % 1, 2) == 0.5 and line_val not in seen_lines:
            seen_lines.add(line_val)
            total_lines.append(OddsLine(
                line=line_val,
                over_odds=over_odds[line_val],
                under_odds=under_odds[line_val],
                source="bwin",
            ))


def _parse_moneyline(options: list[dict]) -> MoneylineOdds | None:
    """Parse 1X2 moneyline options."""
    home_odds = draw_odds = away_odds = 0.0

    for opt in options:
        opt_name = opt.get("name", {}).get("value", "").strip()
        price = opt.get("price", {})
        dec_odds = price.get("odds", 0)
        if not dec_odds or dec_odds <= 1.0:
            continue

        if opt_name.upper() == "X" or opt_name.lower() == "draw":
            draw_odds = dec_odds
        elif not home_odds:
            home_odds = dec_odds  # first non-draw option is home
        else:
            away_odds = dec_odds  # second non-draw option is away

    if home_odds > 0 and away_odds > 0:
        return MoneylineOdds(
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            source="bwin",
        )
    return None


def _parse_handicap(
    options: list[dict],
    spreads: list[SpreadLine],
    seen_handicaps: set[float],
) -> None:
    """Parse handicap/spread options.

    Each bwin "Goals Handicap X" market has exactly 2 options: home and away.
    The option names contain the team + handle in parens, then the handicap
    value in parens, e.g. "Barcelona (Gula14) (1.5)".  The first option is
    always the home team.
    """
    if len(options) != 2:
        return

    opt_home = options[0]
    opt_away = options[1]

    # Extract handicap from the last parenthesized number in each option name
    hc_home_matches = _HANDICAP_RE.findall(opt_home.get("name", {}).get("value", ""))
    hc_away_matches = _HANDICAP_RE.findall(opt_away.get("name", {}).get("value", ""))
    if not hc_home_matches or not hc_away_matches:
        return

    home_hc = float(hc_home_matches[-1])
    home_odds = opt_home.get("price", {}).get("odds", 0)
    away_odds = opt_away.get("price", {}).get("odds", 0)

    if not home_odds or home_odds <= 1.0 or not away_odds or away_odds <= 1.0:
        return

    # Only accept .5 handicaps (standard spread lines)
    if round(abs(home_hc) % 1, 2) != 0.5:
        return

    if home_hc not in seen_handicaps:
        seen_handicaps.add(home_hc)
        spreads.append(SpreadLine(
            handicap=home_hc,
            home_odds=home_odds,
            away_odds=away_odds,
            source="bwin",
        ))


# ── Event matching ────────────────────────────────────────────────


def _find_matching_fixture(
    match: UpcomingMatch,
    fixtures: list[_BwinFixture],
) -> _BwinFixture | None:
    """Find the bwin fixture that corresponds to our UpcomingMatch.

    bwin uses the same "Team (Handle)" naming as BetsAPI/Kambi, so we
    match on player handles (case-insensitive) and start time within
    a tolerance window.
    """
    our_home = extract_handle(match.home).lower()
    our_away = extract_handle(match.away).lower()
    our_pair = frozenset([our_home, our_away])

    best: _BwinFixture | None = None
    best_delta = _TIME_TOLERANCE + 1

    for fix in fixtures:
        bwin_home = extract_handle(fix.home).lower()
        bwin_away = extract_handle(fix.away).lower()
        bwin_pair = frozenset([bwin_home, bwin_away])

        if our_pair != bwin_pair:
            continue

        delta = abs(fix.start_time - match.start_time)
        if delta <= _TIME_TOLERANCE and delta < best_delta:
            best = fix
            best_delta = delta

    return best
