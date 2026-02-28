"""HUDstats API client for GG League eSoccer match schedules.

HUDstats (hudstats.com) powers the H2H Global Gaming League (h2hggl.com)
statistics platform for SIS eSoccer tournaments.  Their public API provides
upcoming schedules, live scores, and player rosters for GG League (8-min)
matches — the same data that feeds bet365 and other sportsbooks.

Unlike BetsAPI which only lists GG League matches ~2 minutes before kickoff,
HUDstats shows the full schedule 30+ minutes ahead, making it an excellent
primary schedule source for GG League alongside Kambi.

API base: https://api-h2h.hudstats.com/
Sport param: "fifa" (mapped from "esoccer" in their SPA)

Key endpoints:
    GET /v1/schedule/upcoming/fifa   — next ~10 upcoming matches
    GET /v1/live/fifa                — currently live matches with scores
    GET /v1/participant/fifa/names   — all player codenames
    GET /v1/schedule/fifa?date=...   — full day schedule (ISO 8601 with TZ)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from esporf.models import UpcomingMatch

logger = logging.getLogger(__name__)

_API_BASE = "https://api-h2h.hudstats.com"

# HUDstats uses "fifa" as the sport identifier for eSoccer
_SPORT = "fifa"

# BetsAPI league ID for GG League 8min (2025 season)
_GG_LEAGUE_ID = 42648

# Cache TTL for schedule data (seconds)
_SCHEDULE_CACHE_TTL = 45


@dataclass
class LiveScore:
    """Snapshot of a live GG League match with current scores."""

    match_id: str
    home: str
    away: str
    home_score: int
    away_score: int
    start_time: int
    league_id: int = _GG_LEAGUE_ID


class HUDstatsClient:
    """Async client for the HUDstats / H2HGGL eSoccer API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[UpcomingMatch] = []
        self._cache_time: float = 0
        # Current live match scores — populated each get_schedule() call.
        # Used by the bot to detect when matches finish and capture results.
        self.live_scores: dict[str, LiveScore] = {}

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
                    "Origin": "https://h2hggl.com",
                    "Referer": "https://h2hggl.com/",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_schedule(self) -> list[UpcomingMatch]:
        """Fetch upcoming GG League matches from HUDstats.

        Returns UpcomingMatch objects with player names formatted as
        "TEAM (HANDLE)" to match the sportsbook convention used by
        BetsAPI and Kambi.

        Results are cached for _SCHEDULE_CACHE_TTL seconds to avoid
        hammering the API across multiple scan phases.
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < _SCHEDULE_CACHE_TTL:
            return self._cache

        client = await self._get_client()

        # Fetch both upcoming and live in parallel-ish (sequential here
        # because the API is fast and we want simple error handling)
        matches: list[UpcomingMatch] = []

        try:
            resp = await client.get(
                f"{_API_BASE}/v1/schedule/upcoming/{_SPORT}"
            )
            resp.raise_for_status()
            upcoming = resp.json()
            if isinstance(upcoming, list):
                for m in upcoming:
                    parsed = _parse_match(m)
                    if parsed:
                        matches.append(parsed)
        except Exception as e:
            logger.warning("HUDstats upcoming fetch failed: %s", e)

        try:
            resp = await client.get(f"{_API_BASE}/v1/live/{_SPORT}")
            resp.raise_for_status()
            live = resp.json()
            current_live: dict[str, LiveScore] = {}
            if isinstance(live, list):
                seen_ids = {m.match_id for m in matches}
                for m in live:
                    parsed = _parse_match(m, is_live=True)
                    if parsed:
                        if parsed.match_id not in seen_ids:
                            matches.append(parsed)
                        # Capture live scores for finished-match detection
                        score = _parse_live_score(m)
                        if score:
                            current_live[score.match_id] = score
            self.live_scores = current_live
        except Exception as e:
            logger.warning("HUDstats live fetch failed: %s", e)

        matches.sort(key=lambda x: x.start_time)

        if matches:
            logger.info(
                "HUDstats: %d GG League match(es) (next %d min)",
                len(matches),
                max(0, (matches[-1].start_time - int(now)) // 60),
            )

        self._cache = matches
        self._cache_time = now
        return matches


def _parse_match(
    data: dict, *, is_live: bool = False,
) -> UpcomingMatch | None:
    """Parse a single HUDstats match object into an UpcomingMatch.

    HUDstats returns:
        {
            "externalId": "FI052260226",
            "startDate": "2026-02-26T04:38:00Z",
            "isCancelled": false,
            "teamAName": "FC BARCELONA",
            "teamBName": "SSC NAPOLI",
            "participantAName": "EXECUTIONER",
            "participantBName": "FAITH",
            "streamName": "Esoccer 1",
            "tournamentName": "Esoccer H2H GG League",
            "matchStatus": null,
            "teamAScore": null,
            "teamBScore": null
        }
    """
    if data.get("isCancelled"):
        return None

    external_id = data.get("externalId", "")
    if not external_id:
        return None

    team_a = data.get("teamAName", "")
    team_b = data.get("teamBName", "")
    player_a = data.get("participantAName", "")
    player_b = data.get("participantBName", "")

    if not player_a or not player_b:
        return None

    # Format: "FC BARCELONA (EXECUTIONER)" to match sportsbook style
    home = f"{team_a} ({player_a})" if team_a else player_a
    away = f"{team_b} ({player_b})" if team_b else player_b

    # Parse ISO 8601 start time
    start_str = data.get("startDate", "")
    try:
        dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        start_time = int(dt.timestamp())
    except (ValueError, TypeError):
        return None

    # Detect live status from the data
    status = data.get("status", data.get("matchStatus", ""))
    if status == "live" or is_live:
        is_live = True

    return UpcomingMatch(
        match_id=f"hudstats_{external_id}",
        league_id=_GG_LEAGUE_ID,
        home=home,
        away=away,
        start_time=start_time,
        is_live=is_live,
    )


def _parse_live_score(data: dict) -> LiveScore | None:
    """Extract a live score snapshot from a HUDstats live match object.

    Returns None if scores aren't available yet (both null/missing).
    """
    external_id = data.get("externalId", "")
    if not external_id:
        return None

    home_score = data.get("teamAScore")
    away_score = data.get("teamBScore")

    # Both scores must be present (non-null) for a valid snapshot
    if home_score is None or away_score is None:
        return None

    team_a = data.get("teamAName", "")
    team_b = data.get("teamBName", "")
    player_a = data.get("participantAName", "")
    player_b = data.get("participantBName", "")

    if not player_a or not player_b:
        return None

    home = f"{team_a} ({player_a})" if team_a else player_a
    away = f"{team_b} ({player_b})" if team_b else player_b

    start_str = data.get("startDate", "")
    try:
        dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        start_time = int(dt.timestamp())
    except (ValueError, TypeError):
        return None

    return LiveScore(
        match_id=f"hudstats_{external_id}",
        home=home,
        away=away,
        home_score=int(home_score),
        away_score=int(away_score),
        start_time=start_time,
    )
