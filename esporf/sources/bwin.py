"""bwin odds client for eSoccer Volta matches.

Fetches real odds from bwin's internal CDS API — the same JSON endpoints
their frontend uses.  This gives us Over/Under and moneyline prices for
Volta matches directly from the book.

Auth: bwin's CDS API requires an ``x-bwin-accessid`` token.  The token
is embedded in the bwin frontend's JavaScript and rotates periodically.
We try to extract it automatically from the page; if that fails, fall
back to a manually configured token in ``.env``.

Endpoint:
    https://cds-api.bwin.com/bettingoffer/fixtures
    ?x-bwin-accessid={token}
    &lang=en&country=GB&userCountry=GB
    &sportIds=108          ← eSoccer
    &fixtureTypes=Standard
    &state=Latest
    &offerMapping=Filtered
    &offerCategories=Gridable
    &fixtureCategories=Gridable
    &skip=0&take=100
    &sortBy=StartDate
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from esporf.config import settings
from esporf.models import MatchOdds, MoneylineOdds, OddsLine, UpcomingMatch, extract_handle

logger = logging.getLogger(__name__)

# bwin sport ID for eSoccer
_SPORT_ID = 108

# CDS API base — region can vary (bwin.com, bwin.fr, bwin.de …)
_CDS_BASE = "https://cds-api.bwin.com"

# How long a fetched token stays valid before we refresh (30 min)
_TOKEN_TTL = 1800

# How long fetched odds stay cached (15 seconds — fast refresh for early odds)
_ODDS_CACHE_TTL = 15

# ── Market name patterns for parsing bwin's offer names ──────────────
# bwin labels Over/Under markets like "Over/Under 4.5" or "Total Goals Over/Under"
_OU_LINE_RE = re.compile(
    r"(?:over|under)\s*/?\s*(?:under|over)?\s*(\d+\.5)",
    re.IGNORECASE,
)
# Individual outcome names: "Over 4.5", "Under 3.5"
_OUTCOME_RE = re.compile(r"(over|under)\s+(\d+\.5)", re.IGNORECASE)

# Moneyline / 1X2 outcome names
_ML_HOME_RE = re.compile(r"^1$|home|player\s*1", re.IGNORECASE)
_ML_DRAW_RE = re.compile(r"^X$|draw", re.IGNORECASE)
_ML_AWAY_RE = re.compile(r"^2$|away|player\s*2", re.IGNORECASE)


class BwinClient:
    """Async client for bwin's internal CDS API (eSoccer odds)."""

    def __init__(self, token: str | None = None) -> None:
        self._configured_token = token or getattr(settings, "bwin_token", "")
        self._live_token: str = ""
        self._token_fetched_at: float = 0
        self._client: httpx.AsyncClient | None = None

        # Cache: list of (fixture_dict, MatchOdds) from last fetch
        self._odds_cache: list[tuple[dict, MatchOdds]] = []
        self._cache_time: float = 0

    @property
    def _token(self) -> str:
        """Return the best available token (live-extracted or configured)."""
        if self._live_token and (time.time() - self._token_fetched_at) < _TOKEN_TTL:
            return self._live_token
        return self._configured_token

    @property
    def is_configured(self) -> bool:
        """True if we have any token to try (configured or previously fetched)."""
        return bool(self._configured_token or self._live_token)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.bwin.com/",
                    "Origin": "https://www.bwin.com",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Token acquisition ─────────────────────────────────────────────

    async def refresh_token(self) -> bool:
        """Try to extract a fresh access token from bwin's frontend.

        bwin's SPA embeds the ``x-bwin-accessid`` in its JavaScript
        config or in early API calls.  We fetch the eSoccer page and
        look for the token in the HTML/JS source.

        Returns True if a new token was acquired.
        """
        client = await self._get_client()
        try:
            resp = await client.get(
                "https://www.bwin.com/en/sports/esoccer-108",
                follow_redirects=True,
            )
            if resp.status_code != 200:
                logger.debug("bwin page returned %d", resp.status_code)
                return False

            text = resp.text

            # Look for the accessId in various patterns:
            # 1. x-bwin-accessid=TOKEN in script/config
            # 2. "accessId":"TOKEN" in JSON config
            # 3. accessid=TOKEN in any URL
            patterns = [
                re.compile(r'x-bwin-accessid[=:]\s*["\']?([a-zA-Z0-9_-]{10,})', re.IGNORECASE),
                re.compile(r'"accessId"\s*:\s*"([a-zA-Z0-9_-]{10,})"'),
                re.compile(r'accessid[=:]\s*["\']?([a-zA-Z0-9_-]{10,})', re.IGNORECASE),
            ]
            for pat in patterns:
                m = pat.search(text)
                if m:
                    self._live_token = m.group(1)
                    self._token_fetched_at = time.time()
                    logger.info("bwin: extracted fresh access token")
                    return True

            logger.warning("bwin: could not find access token in page source")
            return False

        except Exception as e:
            logger.debug("bwin: token refresh failed: %s", e)
            return False

    # ── Odds fetching ─────────────────────────────────────────────────

    async def fetch_esoccer_odds(self) -> list[tuple[dict, MatchOdds]]:
        """Fetch all current eSoccer fixtures with odds from bwin.

        Returns a list of (raw_fixture, MatchOdds) tuples.
        The raw fixture dict contains name, startDate, participants etc.
        for matching against our UpcomingMatch objects.
        """
        now = time.time()
        if self._odds_cache and (now - self._cache_time) < _ODDS_CACHE_TTL:
            return self._odds_cache

        # Ensure we have a token
        token = self._token
        if not token:
            acquired = await self.refresh_token()
            if not acquired:
                logger.warning(
                    "bwin: no access token available — set BWIN_TOKEN in .env "
                    "or check if bwin.com is reachable"
                )
                return []
            token = self._token

        client = await self._get_client()
        params = {
            "x-bwin-accessid": token,
            "lang": "en",
            "country": "GB",
            "userCountry": "GB",
            "sportIds": str(_SPORT_ID),
            "fixtureTypes": "Standard",
            "state": "Latest",
            "offerMapping": "Filtered",
            "offerCategories": "Gridable",
            "fixtureCategories": "Gridable",
            "skip": "0",
            "take": "100",
            "sortBy": "StartDate",
        }

        try:
            resp = await client.get(
                f"{_CDS_BASE}/bettingoffer/fixtures",
                params=params,
            )
            if resp.status_code == 401 or resp.status_code == 403:
                # Token expired — try to refresh
                logger.info("bwin: token rejected (%d), refreshing", resp.status_code)
                if await self.refresh_token():
                    params["x-bwin-accessid"] = self._token
                    resp = await client.get(
                        f"{_CDS_BASE}/bettingoffer/fixtures",
                        params=params,
                    )
                else:
                    return []

            resp.raise_for_status()
            data = resp.json()

        except Exception as e:
            logger.warning("bwin: odds fetch failed: %s", e)
            return []

        fixtures = data.get("fixtures", [])
        results: list[tuple[dict, MatchOdds]] = []

        for fixture in fixtures:
            odds = self._parse_fixture_odds(fixture)
            if odds and odds.has_data:
                results.append((fixture, odds))

        self._odds_cache = results
        self._cache_time = now

        if results:
            logger.info("bwin: fetched odds for %d eSoccer fixtures", len(results))
        else:
            logger.debug("bwin: no fixtures with parseable odds (got %d fixtures total)", len(fixtures))

        return results

    async def attach_odds(self, matches: list[UpcomingMatch]) -> int:
        """Fetch bwin odds and attach to matching UpcomingMatch objects.

        Matches are paired by player names (handles) and approximate
        start time.  Only attaches if the match doesn't already have
        odds from another source.

        Returns the number of matches that got bwin odds attached.
        """
        bwin_fixtures = await self.fetch_esoccer_odds()
        if not bwin_fixtures:
            return 0

        attached = 0
        for match in matches:
            if match.odds and match.odds.has_data:
                continue  # already has odds from BetsAPI etc.

            best = self._find_matching_fixture(match, bwin_fixtures)
            if best:
                fixture, odds = best
                match.odds = odds
                attached += 1
                logger.info(
                    "bwin odds for %s: lines=%s, ML=%s",
                    match.display_name,
                    odds.available_lines,
                    "yes" if odds.moneyline else "no",
                )

        return attached

    # ── Fixture matching ──────────────────────────────────────────────

    @staticmethod
    def _find_matching_fixture(
        match: UpcomingMatch,
        bwin_fixtures: list[tuple[dict, MatchOdds]],
    ) -> tuple[dict, MatchOdds] | None:
        """Find the bwin fixture that corresponds to our UpcomingMatch.

        bwin names might be formatted differently from BetsAPI/AceOdds,
        so we match on player handles (case-insensitive) and start time
        within a 10-minute window.
        """
        our_home = extract_handle(match.home).lower()
        our_away = extract_handle(match.away).lower()
        our_pair = frozenset([our_home, our_away])

        for fixture, odds in bwin_fixtures:
            # Parse participant names from bwin fixture
            participants = fixture.get("participants", [])
            if len(participants) < 2:
                # Try alternative name field
                name = fixture.get("name", {})
                if isinstance(name, dict):
                    name = name.get("value", "")
                elif not isinstance(name, str):
                    name = str(name)
                bwin_handles = _extract_handles_from_name(name)
            else:
                bwin_handles = []
                for p in participants:
                    pname = p.get("name", {})
                    if isinstance(pname, dict):
                        pname = pname.get("value", "")
                    elif not isinstance(pname, str):
                        pname = str(pname)
                    handle = extract_handle(pname).lower() or pname.lower().strip()
                    if handle:
                        bwin_handles.append(handle)

            if len(bwin_handles) < 2:
                continue

            bwin_pair = frozenset(bwin_handles[:2])

            if our_pair != bwin_pair:
                continue

            # Check start time is within 10 minutes
            start_str = fixture.get("startDate", "")
            if start_str:
                try:
                    bwin_ts = int(
                        datetime.fromisoformat(
                            start_str.replace("Z", "+00:00")
                        ).timestamp()
                    )
                    if abs(bwin_ts - match.start_time) > 600:
                        continue
                except (ValueError, TypeError):
                    pass  # can't parse time — still match on names

            return (fixture, odds)

        return None

    # ── Response parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_fixture_odds(fixture: dict) -> MatchOdds | None:
        """Parse bwin fixture JSON into our MatchOdds model.

        bwin's fixture structure (observed):
        {
            "id": "12345",
            "name": {"value": "Team A (PlayerX) - Team B (PlayerY)"},
            "startDate": "2025-01-15T14:30:00Z",
            "participants": [...],
            "games": [
                {
                    "name": {"value": "Over/Under 4.5"},
                    "id": "...",
                    "results": [
                        {"name": {"value": "Over 4.5"}, "odds": 1.85, ...},
                        {"name": {"value": "Under 4.5"}, "odds": 1.95, ...}
                    ]
                },
                {
                    "name": {"value": "1X2"},
                    "results": [
                        {"name": {"value": "1"}, "odds": 2.10, ...},
                        {"name": {"value": "X"}, "odds": 3.40, ...},
                        {"name": {"value": "2"}, "odds": 3.00, ...}
                    ]
                }
            ]
        }
        """
        games = fixture.get("games", [])
        if not games:
            # Some fixtures nest under "optionMarkets" or "markets"
            games = fixture.get("optionMarkets", [])
        if not games:
            games = fixture.get("markets", [])
        if not games:
            return None

        total_lines: list[OddsLine] = []
        moneyline: MoneylineOdds | None = None

        for game in games:
            game_name = _get_name(game)
            results = game.get("results", [])
            if not results:
                results = game.get("outcomes", [])

            # ── Over/Under markets ──
            if _is_ou_market(game_name):
                line_val = _extract_line_value(game_name)
                over_odds = None
                under_odds = None

                for result in results:
                    rname = _get_name(result)
                    odds_val = _get_odds(result)
                    if odds_val is None:
                        continue

                    m = _OUTCOME_RE.search(rname)
                    if m:
                        direction = m.group(1).lower()
                        result_line = float(m.group(2))
                        if line_val is None:
                            line_val = result_line
                        if direction == "over":
                            over_odds = odds_val
                        elif direction == "under":
                            under_odds = odds_val
                    elif "over" in rname.lower():
                        over_odds = odds_val
                    elif "under" in rname.lower():
                        under_odds = odds_val

                if line_val is not None and over_odds and under_odds:
                    total_lines.append(OddsLine(
                        line=line_val,
                        over_odds=over_odds,
                        under_odds=under_odds,
                        source="bwin",
                    ))

            # ── 1X2 / Moneyline ──
            elif _is_ml_market(game_name):
                home_odds = draw_odds = away_odds = None

                for result in results:
                    rname = _get_name(result)
                    odds_val = _get_odds(result)
                    if odds_val is None:
                        continue

                    if _ML_HOME_RE.search(rname):
                        home_odds = odds_val
                    elif _ML_DRAW_RE.search(rname):
                        draw_odds = odds_val
                    elif _ML_AWAY_RE.search(rname):
                        away_odds = odds_val

                if home_odds and draw_odds and away_odds:
                    moneyline = MoneylineOdds(
                        home_odds=home_odds,
                        draw_odds=draw_odds,
                        away_odds=away_odds,
                        source="bwin",
                    )

        if not total_lines and not moneyline:
            return None

        return MatchOdds(total_lines=total_lines, moneyline=moneyline)


# ── Helpers ───────────────────────────────────────────────────────────

def _get_name(obj: dict) -> str:
    """Extract display name from bwin's {name: {value: "..."}} pattern."""
    name = obj.get("name", "")
    if isinstance(name, dict):
        return name.get("value", "")
    return str(name) if name else ""


def _get_odds(result: dict) -> float | None:
    """Extract decimal odds from a result/outcome dict."""
    # bwin uses various keys: "odds", "price", "decimal"
    for key in ("odds", "price", "decimal"):
        val = result.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 1.0:
                    return f
            except (ValueError, TypeError):
                continue
    # Nested: {"price": {"odds": 1.85}}
    price = result.get("price", {})
    if isinstance(price, dict):
        for key in ("odds", "decimal", "value"):
            val = price.get(key)
            if val is not None:
                try:
                    f = float(val)
                    if f > 1.0:
                        return f
                except (ValueError, TypeError):
                    continue
    return None


def _is_ou_market(name: str) -> bool:
    """Check if a market name is an Over/Under total goals market."""
    lower = name.lower()
    return ("over" in lower and "under" in lower) or "total" in lower


def _is_ml_market(name: str) -> bool:
    """Check if a market name is a 1X2 / moneyline market."""
    lower = name.lower()
    return lower in ("1x2", "match result", "moneyline", "match winner") or "1 x 2" in lower


def _extract_line_value(market_name: str) -> float | None:
    """Extract the line value from market name like 'Over/Under 4.5'."""
    m = _OU_LINE_RE.search(market_name)
    return float(m.group(1)) if m else None


def _extract_handles_from_name(match_name: str) -> list[str]:
    """Extract player handles from a match name like 'Team (PlayerX) - Team (PlayerY)'.

    Handles various separators: ' - ', ' v ', ' vs '.
    """
    # Try "(Handle)" pattern first
    handles = re.findall(r"\(([^)]+)\)", match_name)
    if len(handles) >= 2:
        return [h.strip().lower() for h in handles[:2]]

    # Fallback: split on common separators and use full names
    for sep in (" - ", " v ", " vs ", " – "):
        if sep in match_name:
            parts = match_name.split(sep, 1)
            return [p.strip().lower() for p in parts]

    return []
