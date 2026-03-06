"""FanDuel sportsbook API client for eSoccer odds.

FanDuel's API is publicly accessible (no authentication beyond a static API key
baked into their frontend) and returns eSoccer events with rich market data:
13 market types including Over/Under, 1X2, spreads, BTTS, and more.

This serves as a supplementary odds source — especially useful for GG League
matches when Kambi or BetsAPI odds are delayed or unavailable.

Endpoint:
    https://sbapi.{state}.sportsbook.fanduel.com/api/
        content-managed-page?page=SPORT&eventTypeId=1
        &_ak=FhMFpcPWXMeyZxOx

States: nj, mi, pa, co, il, in, ia, ks, la, md, oh, tn, va, wv, wy, az
"""

from __future__ import annotations

import logging
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
    match_by_handles,
)

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────

# FanDuel state-specific API base
_API_BASE = "https://sbapi.nj.sportsbook.fanduel.com/api"

# Public API key (embedded in FanDuel's frontend JavaScript)
_API_KEY = "FhMFpcPWXMeyZxOx"

# Soccer event type ID (eSoccer lives under Soccer on FanDuel)
_SOCCER_EVENT_TYPE = 1

# Known eSoccer competition ID on FanDuel
_ESOCCER_COMPETITION_ID = 12730404  # eSoccer H2H GG League 2x4mins

# Cache TTL for fetched data (seconds)
_CACHE_TTL = 45

# Maximum time difference (seconds) when matching events by start time
_TIME_TOLERANCE = 600  # 10 minutes

# FanDuel eSoccer → BetsAPI league ID mapping
# FanDuel doesn't distinguish leagues the way BetsAPI does, so we map
# the competition to the most likely league. The bot's matching logic
# uses player handles + start time, so mismatched league IDs are caught.
_FANDUEL_LEAGUE_ID = 42648  # GG League (8min / 2x4min format)


class FanDuelClient:
    """Async client for FanDuel's public sportsbook API — eSoccer odds."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[_FanDuelEvent] = []
        self._cache_time: float = 0

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
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Public methods ───────────────────────────────────────────

    async def attach_odds(self, matches: list[UpcomingMatch]) -> int:
        """Fetch FanDuel eSoccer odds and attach to matching UpcomingMatch objects.

        Only attaches if the match doesn't already have odds.  Matches by
        player handles (case-insensitive) and approximate start time.

        Returns the number of matches that received FanDuel odds.
        """
        events = await self._fetch_esoccer_events()
        if not events:
            return 0

        attached = 0
        for match in matches:
            if match.odds and match.odds.has_data:
                continue  # already has odds

            best = _find_matching_event(match, events)
            if best and best.odds and best.odds.has_data:
                match.odds = best.odds
                attached += 1
                logger.info(
                    "fanduel odds for %s: lines=%s, ML=%s, spreads=%s",
                    match.display_name,
                    best.odds.available_lines,
                    "yes" if best.odds.moneyline else "no",
                    [s.handicap for s in best.odds.spreads] if best.odds.spreads else "no",
                )

        return attached

    # ── Data fetching ──────────────────────────────────────────────

    async def _fetch_esoccer_events(self) -> list[_FanDuelEvent]:
        """Fetch all eSoccer events with odds from FanDuel.

        Results are cached for _CACHE_TTL seconds.
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < _CACHE_TTL:
            return self._cache

        client = await self._get_client()
        url = f"{_API_BASE}/content-managed-page"
        params = {
            "page": "SPORT",
            "eventTypeId": str(_SOCCER_EVENT_TYPE),
            "_ak": _API_KEY,
        }

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("fanduel: eSoccer fetch failed: %s", e)
            return self._cache

        events = _parse_response(data)
        self._cache = events
        self._cache_time = now

        if events:
            with_odds = sum(1 for ev in events if ev.odds and ev.odds.has_data)
            logger.info(
                "fanduel: fetched %d eSoccer events (%d with odds)",
                len(events), with_odds,
            )
        else:
            logger.debug("fanduel: no eSoccer events returned")

        return events


# ── Internal data structures ──────────────────────────────────────


class _FanDuelEvent:
    """Parsed FanDuel event with odds."""

    __slots__ = ("event_id", "home", "away", "start_time", "competition_id", "odds")

    def __init__(
        self,
        event_id: str,
        home: str,
        away: str,
        start_time: int,
        competition_id: int,
        odds: MatchOdds | None,
    ) -> None:
        self.event_id = event_id
        self.home = home
        self.away = away
        self.start_time = start_time
        self.competition_id = competition_id
        self.odds = odds


# ── Response parsing ──────────────────────────────────────────────


def _parse_response(data: dict) -> list[_FanDuelEvent]:
    """Parse the FanDuel content-managed-page response into events with odds."""
    attachments = data.get("attachments", {})
    raw_events = attachments.get("events", {})
    raw_markets = attachments.get("markets", {})

    # Only keep eSoccer events (competition 12730404)
    esoccer_events: dict[str, dict] = {}
    for eid, ev in raw_events.items():
        comp_id = ev.get("competitionId")
        if comp_id == _ESOCCER_COMPETITION_ID:
            esoccer_events[str(eid)] = ev

    if not esoccer_events:
        return []

    # Group markets by event ID
    markets_by_event: dict[str, list[dict]] = {}
    for mid, mkt in raw_markets.items():
        mkt_event_id = str(mkt.get("eventId", ""))
        if mkt_event_id in esoccer_events:
            markets_by_event.setdefault(mkt_event_id, []).append(mkt)

    results: list[_FanDuelEvent] = []
    for eid, ev in esoccer_events.items():
        name = ev.get("name", "")
        if " v " not in name and " vs " not in name:
            continue

        # Parse "Team A (Handle) v Team B (Handle)" format
        separator = " v " if " v " in name else " vs "
        parts = name.split(separator, 1)
        if len(parts) != 2:
            continue

        home = parts[0].strip()
        away = parts[1].strip()

        # Parse start time
        open_date = ev.get("openDate", "")
        try:
            dt = datetime.fromisoformat(open_date.replace("Z", "+00:00"))
            start_ts = int(dt.timestamp())
        except (ValueError, TypeError):
            continue

        odds = _parse_event_markets(markets_by_event.get(eid, []))

        results.append(_FanDuelEvent(
            event_id=eid,
            home=home,
            away=away,
            start_time=start_ts,
            competition_id=_ESOCCER_COMPETITION_ID,
            odds=odds,
        ))

    return results


def _parse_event_markets(markets: list[dict]) -> MatchOdds | None:
    """Parse all markets for a single event into MatchOdds."""
    total_lines: list[OddsLine] = []
    moneyline: MoneylineOdds | None = None
    spreads: list[SpreadLine] = []
    seen_lines: set[float] = set()
    seen_handicaps: set[float] = set()

    for mkt in markets:
        mkt_type = mkt.get("marketType", "")
        runners = mkt.get("runners", [])

        if mkt_type == "TOTAL_GOALS":
            _parse_fd_total(runners, total_lines, seen_lines)
        elif mkt_type == "WIN-DRAW-WIN":
            ml = _parse_fd_moneyline(runners)
            if ml:
                moneyline = ml
        elif mkt_type == "2-WAY_HANDICAP_BETTING":
            _parse_fd_handicap(runners, spreads, seen_handicaps)

    if not total_lines and not moneyline and not spreads:
        return None

    return MatchOdds(total_lines=total_lines, moneyline=moneyline, spreads=spreads)


def _get_decimal_odds(runner: dict) -> float:
    """Extract decimal odds from a FanDuel runner."""
    win_odds = runner.get("winRunnerOdds", {})
    true_odds = win_odds.get("trueOdds", {})
    dec_section = true_odds.get("decimalOdds", {})
    dec_val = dec_section.get("decimalOdds", 0)
    if dec_val and dec_val > 1.0:
        return float(dec_val)
    return 0.0


def _parse_fd_total(
    runners: list[dict],
    total_lines: list[OddsLine],
    seen_lines: set[float],
) -> None:
    """Parse FanDuel Over/Under runners."""
    over_data: dict[float, float] = {}
    under_data: dict[float, float] = {}

    for runner in runners:
        name = runner.get("runnerName", "").lower()
        handicap = runner.get("handicap")
        dec_odds = _get_decimal_odds(runner)
        if not dec_odds or handicap is None:
            continue

        line = float(handicap)
        if "over" in name:
            over_data[line] = dec_odds
        elif "under" in name:
            under_data[line] = dec_odds

    for line_val in sorted(set(over_data.keys()) & set(under_data.keys())):
        if round(line_val % 1, 2) == 0.5 and line_val not in seen_lines:
            seen_lines.add(line_val)
            total_lines.append(OddsLine(
                line=line_val,
                over_odds=over_data[line_val],
                under_odds=under_data[line_val],
                source="fanduel",
            ))


def _parse_fd_moneyline(runners: list[dict]) -> MoneylineOdds | None:
    """Parse FanDuel 1X2 moneyline runners."""
    home_odds = draw_odds = away_odds = 0.0

    for runner in runners:
        result = runner.get("result", {})
        rtype = result.get("type", "")
        dec_odds = _get_decimal_odds(runner)
        if not dec_odds:
            continue

        if rtype == "HOME":
            home_odds = dec_odds
        elif rtype == "DRAW":
            draw_odds = dec_odds
        elif rtype == "AWAY":
            away_odds = dec_odds

    if home_odds > 0 and away_odds > 0:
        return MoneylineOdds(
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            source="fanduel",
        )
    return None


def _parse_fd_handicap(
    runners: list[dict],
    spreads: list[SpreadLine],
    seen_handicaps: set[float],
) -> None:
    """Parse FanDuel 2-way handicap runners."""
    home_odds = away_odds = 0.0
    handicap = None

    for runner in runners:
        result = runner.get("result", {})
        rtype = result.get("type", "")
        hc = runner.get("handicap")
        dec_odds = _get_decimal_odds(runner)
        if not dec_odds or hc is None:
            continue

        if rtype == "HOME":
            home_odds = dec_odds
            handicap = float(hc)
        elif rtype == "AWAY":
            away_odds = dec_odds

    if (
        handicap is not None
        and home_odds > 0
        and away_odds > 0
        and round(abs(handicap) % 1, 2) == 0.5
        and handicap not in seen_handicaps
    ):
        seen_handicaps.add(handicap)
        spreads.append(SpreadLine(
            handicap=handicap,
            home_odds=home_odds,
            away_odds=away_odds,
            source="fanduel",
        ))


# ── Event matching ────────────────────────────────────────────────


def _find_matching_event(
    match: UpcomingMatch,
    fd_events: list[_FanDuelEvent],
) -> _FanDuelEvent | None:
    """Find the FanDuel event that corresponds to our UpcomingMatch."""
    return match_by_handles(
        match.home, match.away, match.start_time,
        fd_events, time_tolerance=_TIME_TOLERANCE,
    )
