"""ESportsBattle client for fetching Volta match schedules.

ESportsBattle (football.esportsbattle.com) is the tournament organizer that
runs the eSoccer Volta matches. Their API provides a ~30 minute lookahead
on upcoming matches — far better than BetsAPI's ~2 minute window.

This is used as the PRIMARY schedule source. BetsAPI is still used for
historical results, odds, and match IDs for cross-referencing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from esporf.models import UpcomingMatch

logger = logging.getLogger(__name__)

_BASE_URL = "https://football.esportsbattle.com/api"

# Location ID for Volta matches on ESportsBattle
_VOLTA_LOCATION = "Hillsborough"

# BetsAPI league ID for Volta (used when creating UpcomingMatch objects)
_VOLTA_LEAGUE_ID = 38439


class ESportsBattleClient:
    """Client for the ESportsBattle REST API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

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

            p1 = m.get("participant1", {})
            p2 = m.get("participant2", {})

            team1 = p1.get("team", {}).get("token_international", "")
            nick1 = p1.get("nickname", "")
            team2 = p2.get("team", {}).get("token_international", "")
            nick2 = p2.get("nickname", "")

            if not nick1 or not nick2:
                continue

            # Format names to match BetsAPI/sportsbook style: "Team (Handle)"
            home = f"{team1} ({nick1})" if team1 else nick1
            away = f"{team2} ({nick2})" if team2 else nick2

            # Parse start time
            date_str = m.get("date", "")
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                start_time = int(dt.timestamp())
            except (ValueError, TypeError):
                continue

            # Use ESportsBattle match ID prefixed to avoid collision with BetsAPI IDs
            esb_id = str(m.get("id", ""))
            match_id = f"esb_{esb_id}"

            # status_id: 1 = not started, 2 = live, 3 = ended
            status = m.get("status_id", 1)
            is_live = status == 2

            matches.append(
                UpcomingMatch(
                    match_id=match_id,
                    league_id=_VOLTA_LEAGUE_ID,
                    home=home,
                    away=away,
                    start_time=start_time,
                    is_live=is_live,
                )
            )

        matches.sort(key=lambda x: x.start_time)

        if matches:
            logger.info(
                "ESportsBattle: %d Volta matches (next %d min)",
                len(matches),
                max(0, (matches[-1].start_time - int(time.time())) // 60),
            )

        return matches
