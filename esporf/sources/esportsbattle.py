"""ESportsBattle client for fetching Volta match schedules.

ESportsBattle (football.esportsbattle.com) is the tournament organizer that
runs the eSoccer Volta matches.  Their API provides two levels of lookahead:

1. ``/tournaments/nearest-matches`` — next ~30 min (fast, single call)
2. ``/participants/{name}/tournaments`` + ``/tournaments/{id}/matches`` —
   full-day deep schedule (all matches for every upcoming tournament).

This is used as the PRIMARY schedule source. BetsAPI is still used for
historical results, odds, and match IDs for cross-referencing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import httpx

from esporf.models import UpcomingMatch

logger = logging.getLogger(__name__)

_BASE_URL = "https://football.esportsbattle.com/api"

# Location code prefix for Volta matches on ESportsBattle
_VOLTA_LOCATION = "Hillsborough"

# BetsAPI league ID for Volta (used when creating UpcomingMatch objects)
_VOLTA_LEAGUE_ID = 38439

# Any Volta player handle — used to discover upcoming tournament IDs
_PROBE_PLAYER = "fantazer"

# Cache TTL for the deep schedule (seconds)
_DEEP_CACHE_TTL = 120


class ESportsBattleClient:
    """Client for the ESportsBattle REST API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._deep_cache: list[UpcomingMatch] = []
        self._deep_cache_time: float = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_nearest_matches(self) -> list[dict[str, Any]]:
        """Fetch the next ~15 upcoming matches across all locations."""
        client = await self._get_client()
        resp = await client.get("/tournaments/nearest-matches")
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        return data

    async def get_volta_schedule(self) -> list[UpcomingMatch]:
        """Fetch upcoming Volta matches from ESportsBattle.

        Filters by the Hillsborough location (where Volta matches run)
        and converts to UpcomingMatch objects compatible with the rest
        of the pipeline.

        Returns matches ~30 minutes ahead with exact matchups and times.
        """
        raw = await self.get_nearest_matches()

        matches: list[UpcomingMatch] = []
        for m in raw:
            location = m.get("location", {}).get("token_international", "")
            if location != _VOLTA_LOCATION:
                continue

            # Extra guard: if tournament info is present, verify it's Volta
            tournament = m.get("tournament", {})
            if isinstance(tournament, dict) and tournament:
                tourney_name = str(
                    tournament.get("token_international", "")
                    or tournament.get("name", "")
                ).lower()
                if tourney_name and "volta" not in tourney_name:
                    logger.debug(
                        "Skipping ESB match — tournament '%s' is not Volta",
                        tourney_name,
                    )
                    continue

            parsed = _parse_esb_match(m)
            if parsed:
                matches.append(parsed)

        matches.sort(key=lambda x: x.start_time)

        if matches:
            logger.info(
                "ESportsBattle: %d Volta matches (next %d min)",
                len(matches),
                max(0, (matches[-1].start_time - int(time.time())) // 60),
            )

        return matches

    async def get_volta_deep_schedule(self) -> list[UpcomingMatch]:
        """Fetch the full-day Volta schedule via tournament endpoints.

        This queries a Volta player's tournament page to discover all
        upcoming Volta tournament IDs, then fetches the match list for
        each tournament.  Returns matches up to 24+ hours in advance.

        Results are cached for _DEEP_CACHE_TTL seconds to keep API load
        reasonable (this makes several calls per invocation).
        """
        now = time.time()
        if self._deep_cache and (now - self._deep_cache_time) < _DEEP_CACHE_TTL:
            return self._deep_cache

        client = await self._get_client()

        # Step 1: Discover upcoming Volta tournament IDs via a probe player
        try:
            resp = await client.get(
                f"/participants/{_PROBE_PLAYER}/tournaments",
                params={"page": "1"},
            )
            resp.raise_for_status()
            page = resp.json()
        except Exception as e:
            logger.warning("ESportsBattle deep schedule: tournament list failed: %s", e)
            return self._deep_cache  # return stale cache on error

        tournaments = page.get("tournaments", [])

        # Filter for active Volta tournaments (status 2=public, 3=started)
        volta_ids: list[int] = []
        for t in tournaments:
            status = t.get("status_id")
            name = str(
                t.get("token_international", "") or t.get("token", "")
            ).lower()
            if status in (2, 3) and "volta" in name:
                volta_ids.append(t["id"])

        if not volta_ids:
            return self._deep_cache

        # Step 2: Fetch match lists for each Volta tournament
        seen_ids: set[str] = set()
        matches: list[UpcomingMatch] = []

        for tid in volta_ids:
            try:
                resp = await client.get(f"/tournaments/{tid}/matches")
                resp.raise_for_status()
                match_list = resp.json()
            except Exception as e:
                logger.warning(
                    "ESportsBattle deep schedule: tournament %d failed: %s", tid, e,
                )
                continue

            if not isinstance(match_list, list):
                continue

            for m in match_list:
                # Only include planned (1) or live (2) matches
                status = m.get("status_id", 0)
                if status not in (1, 2):
                    continue

                parsed = _parse_esb_match(m)
                if parsed and parsed.match_id not in seen_ids:
                    seen_ids.add(parsed.match_id)
                    matches.append(parsed)

        matches.sort(key=lambda x: x.start_time)

        if matches:
            hours_ahead = max(0, (matches[-1].start_time - int(now)) / 3600)
            logger.info(
                "ESportsBattle deep: %d Volta matches across %d tournaments (%.1fh ahead)",
                len(matches),
                len(volta_ids),
                hours_ahead,
            )

        self._deep_cache = matches
        self._deep_cache_time = now
        return matches


def _parse_esb_match(
    m: dict[str, Any], *, is_live: bool = False,
) -> UpcomingMatch | None:
    """Parse a single ESportsBattle match dict into an UpcomingMatch."""
    p1 = m.get("participant1", {})
    p2 = m.get("participant2", {})

    team1 = p1.get("team", {}).get("token_international", "")
    nick1 = p1.get("nickname", "")
    team2 = p2.get("team", {}).get("token_international", "")
    nick2 = p2.get("nickname", "")

    if not nick1 or not nick2:
        return None

    home = f"{team1} ({nick1})" if team1 else nick1
    away = f"{team2} ({nick2})" if team2 else nick2

    date_str = m.get("date", "")
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        start_time = int(dt.timestamp())
    except (ValueError, TypeError):
        return None

    esb_id = str(m.get("id", ""))
    match_id = f"esb_{esb_id}"

    status = m.get("status_id", 1)
    if status == 2 or is_live:
        is_live = True

    return UpcomingMatch(
        match_id=match_id,
        league_id=_VOLTA_LEAGUE_ID,
        home=home,
        away=away,
        start_time=start_time,
        is_live=is_live,
    )
