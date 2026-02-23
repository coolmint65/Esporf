"""Kambi public API client for eSoccer odds.

Fetches real-time odds from Kambi's public CDN API — the same data that
powers sportsbooks like Unibet, 888sport, and DraftKings.  No API key
or authentication required.

eSoccer is nested under Football > Esports Football on Kambi's platform.
We fetch all eSoccer bet offers and match them to our UpcomingMatch
objects by player handle + approximate start time.

Endpoint:
    https://eu-offering-api.kambicdn.com/offering/v2018/{operator}/
        betoffer/group/{group_id}.json
        ?lang=en_US&market=US

Groups:
    2000124075 = All eSoccer (parent)
    2000124080 = Esports Battle (2x4min) — maps to GG League 8min
    2010205703 = eSports Battle (2x6min) — maps to GT Leagues 12min
    2010205705 = Cyber Live Arena (2x5min)
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from esporf.models import MatchOdds, MoneylineOdds, OddsLine, UpcomingMatch, extract_handle

logger = logging.getLogger(__name__)

# Kambi CDN base URL
_CDN_BASE = "https://eu-offering-api.kambicdn.com/offering/v2018"

# Operator code — Unibet Sweden is the most reliable public operator
_OPERATOR = "ubse"

# eSoccer parent group ID (all sub-leagues)
_ESOCCER_GROUP = 2000124075

# Cache TTL for fetched odds (seconds)
_ODDS_CACHE_TTL = 30

# Maximum time difference (seconds) when matching events by start time
_TIME_TOLERANCE = 600  # 10 minutes


class KambiClient:
    """Async client for Kambi's public eSoccer odds feed."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[_KambiEvent] = []
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

    # ── Main public method ─────────────────────────────────────────

    async def attach_odds(self, matches: list[UpcomingMatch]) -> int:
        """Fetch Kambi odds and attach to matching UpcomingMatch objects.

        Only attaches if the match doesn't already have odds from another
        source. Matches by player handles (case-insensitive) and approximate
        start time.

        Returns the number of matches that received Kambi odds.
        """
        kambi_events = await self._fetch_esoccer_events()
        if not kambi_events:
            return 0

        attached = 0
        for match in matches:
            if match.odds and match.odds.has_data:
                continue  # already has odds

            best = _find_matching_event(match, kambi_events)
            if best and best.odds and best.odds.has_data:
                match.odds = best.odds
                attached += 1
                logger.info(
                    "kambi odds for %s: lines=%s, ML=%s",
                    match.display_name,
                    best.odds.available_lines,
                    "yes" if best.odds.moneyline else "no",
                )

        return attached

    # ── Data fetching ──────────────────────────────────────────────

    async def _fetch_esoccer_events(self) -> list[_KambiEvent]:
        """Fetch all eSoccer events with odds from Kambi CDN.

        Returns parsed event objects with odds attached. Results are
        cached for _ODDS_CACHE_TTL seconds.
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < _ODDS_CACHE_TTL:
            return self._cache

        client = await self._get_client()
        url = f"{_CDN_BASE}/{_OPERATOR}/betoffer/group/{_ESOCCER_GROUP}.json"
        params = {"lang": "en_US", "market": "US"}

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("kambi: eSoccer fetch failed: %s", e)
            return self._cache  # return stale cache on error

        events = _parse_response(data)
        self._cache = events
        self._cache_time = now

        if events:
            with_odds = sum(1 for e in events if e.odds and e.odds.has_data)
            logger.info(
                "kambi: fetched %d eSoccer events (%d with odds)",
                len(events), with_odds,
            )
        else:
            logger.debug("kambi: no eSoccer events returned")

        return events


# ── Internal data structures ──────────────────────────────────────


class _KambiEvent:
    """Parsed Kambi event with odds."""

    __slots__ = ("event_id", "home", "away", "start_time", "group", "odds")

    def __init__(
        self,
        event_id: int,
        home: str,
        away: str,
        start_time: int,
        group: str,
        odds: MatchOdds | None,
    ) -> None:
        self.event_id = event_id
        self.home = home
        self.away = away
        self.start_time = start_time
        self.group = group
        self.odds = odds


# ── Response parsing ──────────────────────────────────────────────


def _parse_response(data: dict) -> list[_KambiEvent]:
    """Parse the full Kambi betoffer/group response into events with odds."""
    raw_events = data.get("events", [])
    raw_offers = data.get("betOffers", [])

    # Index events by ID
    events_by_id: dict[int, dict] = {}
    for ev in raw_events:
        eid = ev.get("id")
        if eid is not None:
            events_by_id[eid] = ev

    # Group bet offers by event ID
    offers_by_event: dict[int, list[dict]] = {}
    for offer in raw_offers:
        eid = offer.get("eventId")
        if eid is not None:
            offers_by_event.setdefault(eid, []).append(offer)

    results: list[_KambiEvent] = []
    for eid, ev in events_by_id.items():
        home = ev.get("homeName", "")
        away = ev.get("awayName", "")
        if not home or not away:
            continue

        # Parse start time
        start_str = ev.get("start", "")
        try:
            dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            start_ts = int(dt.timestamp())
        except (ValueError, TypeError):
            continue

        group = ev.get("group", "")
        odds = _parse_event_odds(offers_by_event.get(eid, []))

        results.append(_KambiEvent(
            event_id=eid,
            home=home,
            away=away,
            start_time=start_ts,
            group=group,
            odds=odds,
        ))

    return results


def _parse_event_odds(offers: list[dict]) -> MatchOdds | None:
    """Parse all bet offers for a single event into MatchOdds."""
    total_lines: list[OddsLine] = []
    moneyline: MoneylineOdds | None = None
    seen_lines: set[float] = set()

    for offer in offers:
        offer_type = offer.get("betOfferType", {}).get("name", "")
        criterion = offer.get("criterion", {})
        crit_label = criterion.get("label", "")
        outcomes = offer.get("outcomes", [])

        # ── Full-time Total Goals Over/Under ──
        if (
            offer_type == "Over/Under"
            and crit_label == "Total Goals"
            and criterion.get("lifetime") == "FULL_TIME"
        ):
            over_odds = None
            under_odds = None
            line_val = None

            for oc in outcomes:
                raw_odds = oc.get("odds")
                raw_line = oc.get("line")
                if raw_odds is None or raw_line is None:
                    continue

                dec_odds = raw_odds / 1000.0
                line = raw_line / 1000.0
                if dec_odds <= 1.0 or line <= 0:
                    continue

                line_val = line
                oc_type = oc.get("type", "")
                if oc_type == "OT_OVER" or "over" in oc.get("label", "").lower():
                    over_odds = dec_odds
                elif oc_type == "OT_UNDER" or "under" in oc.get("label", "").lower():
                    under_odds = dec_odds

            if line_val is not None and over_odds and under_odds and line_val not in seen_lines:
                seen_lines.add(line_val)
                total_lines.append(OddsLine(
                    line=line_val,
                    over_odds=over_odds,
                    under_odds=under_odds,
                    source="kambi",
                ))

        # ── Full-time 1X2 (Regular Time) ──
        elif offer_type == "Match" and "regular" in crit_label.lower():
            home_odds = draw_odds = away_odds = 0.0
            for oc in outcomes:
                raw_odds = oc.get("odds")
                if raw_odds is None:
                    continue
                dec_odds = raw_odds / 1000.0
                if dec_odds <= 1.0:
                    continue

                oc_type = oc.get("type", "")
                if oc_type == "OT_ONE":
                    home_odds = dec_odds
                elif oc_type == "OT_CROSS":
                    draw_odds = dec_odds
                elif oc_type == "OT_TWO":
                    away_odds = dec_odds

            if home_odds > 0 and away_odds > 0:
                moneyline = MoneylineOdds(
                    home_odds=home_odds,
                    draw_odds=draw_odds,
                    away_odds=away_odds,
                    source="kambi",
                )

    if not total_lines and not moneyline:
        return None

    return MatchOdds(total_lines=total_lines, moneyline=moneyline)


# ── Event matching ────────────────────────────────────────────────


def _find_matching_event(
    match: UpcomingMatch,
    kambi_events: list[_KambiEvent],
) -> _KambiEvent | None:
    """Find the Kambi event that corresponds to our UpcomingMatch.

    Kambi uses the same "Team (Handle)" naming format as BetsAPI, so
    we match on player handles (case-insensitive) and start time within
    a tolerance window.
    """
    our_home = extract_handle(match.home).lower()
    our_away = extract_handle(match.away).lower()
    our_pair = frozenset([our_home, our_away])

    best: _KambiEvent | None = None
    best_delta = _TIME_TOLERANCE + 1

    for event in kambi_events:
        kambi_home = extract_handle(event.home).lower()
        kambi_away = extract_handle(event.away).lower()
        kambi_pair = frozenset([kambi_home, kambi_away])

        if our_pair != kambi_pair:
            continue

        delta = abs(event.start_time - match.start_time)
        if delta <= _TIME_TOLERANCE and delta < best_delta:
            best = event
            best_delta = delta

    return best
