"""BetsAPI client for fetching eSoccer match results and upcoming fixtures.

Primary data source for building match history. Fetches:
- Ended matches with scores (for historical trend database)
- Upcoming matches (to know who's playing next and run trend analysis)
- In-play matches (for live alerting)

Docs: https://betsapi.com/docs/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from esporf.config import settings
from esporf.models import MatchResult, UpcomingMatch

logger = logging.getLogger(__name__)

SPORT_ID = 1  # Soccer (eSoccer is categorized under soccer)


class BetsAPIClient:
    """Async client for the BetsAPI REST API."""

    def __init__(self, token: str | None = None, base_url: str | None = None):
        self.token = token or settings.betsapi_token
        self.base_url = base_url or settings.betsapi_base_url
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        if not self.token:
            raise ValueError(
                "BetsAPI token not configured. Set BETSAPI_TOKEN in your .env file. "
                "Get a token at https://betsapi.com/"
            )
        client = await self._get_client()
        all_params = {"token": self.token}
        if params:
            all_params.update(params)

        resp = await client.get(endpoint, params=all_params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("success") == 0:
            error = data.get("error", "Unknown BetsAPI error")
            raise RuntimeError(f"BetsAPI error: {error}")

        return data

    # ── Ended matches (for building history) ─────────────────────────

    async def get_ended_matches(self, league_id: int, page: int = 1) -> list[MatchResult]:
        """Fetch completed matches with final scores for a league.

        Each page returns ~50 results. Use multiple pages to backfill history.
        """
        data = await self._request(
            "/events/ended",
            params={"sport_id": SPORT_ID, "league_id": league_id, "page": page},
        )
        results = []
        for ev in data.get("results", []):
            match = self._parse_ended_match(ev, league_id)
            if match is not None:
                results.append(match)
        return results

    async def backfill_history(self, league_id: int, pages: int = 10) -> list[MatchResult]:
        """Fetch multiple pages of ended matches to build up historical database."""
        all_results: list[MatchResult] = []
        for page in range(1, pages + 1):
            try:
                matches = await self.get_ended_matches(league_id, page=page)
                if not matches:
                    break  # no more results
                all_results.extend(matches)
                logger.info(
                    "Fetched page %d for league %d: %d matches", page, league_id, len(matches)
                )
            except Exception as e:
                logger.warning("Failed to fetch page %d for league %d: %s", page, league_id, e)
                break
        return all_results

    # ── Upcoming matches ─────────────────────────────────────────────

    async def get_upcoming_matches(self, league_id: int) -> list[UpcomingMatch]:
        """Fetch upcoming (pre-match) events for a league."""
        data = await self._request(
            "/events/upcoming",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        return [self._parse_upcoming(ev, league_id) for ev in data.get("results", [])]

    async def get_inplay_matches(self, league_id: int) -> list[UpcomingMatch]:
        """Fetch live / in-play events for a league."""
        data = await self._request(
            "/events/inplay",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        matches = []
        for ev in data.get("results", []):
            m = self._parse_upcoming(ev, league_id)
            m.is_live = True
            matches.append(m)
        return matches

    # ── Parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_ended_match(event: dict, league_id: int) -> MatchResult | None:
        """Parse a BetsAPI ended event into a MatchResult with scores."""
        home_info = event.get("home", {})
        away_info = event.get("away", {})
        scores = event.get("scores", {})

        # Full-time score is typically under key "2"
        ft = scores.get("2", {})
        home_score = _safe_int(ft.get("home"))
        away_score = _safe_int(ft.get("away"))

        if home_score is None or away_score is None:
            return None  # skip matches without valid scores

        # Half-time score under key "1"
        ht = scores.get("1", {})

        return MatchResult(
            match_id=str(event.get("id", "")),
            league_id=league_id,
            home=home_info.get("name", "Unknown"),
            away=away_info.get("name", "Unknown"),
            home_score=home_score,
            away_score=away_score,
            start_time=int(event.get("time", 0)),
            ht_home_score=_safe_int(ht.get("home")),
            ht_away_score=_safe_int(ht.get("away")),
        )

    @staticmethod
    def _parse_upcoming(event: dict, league_id: int) -> UpcomingMatch:
        home_info = event.get("home", {})
        away_info = event.get("away", {})
        return UpcomingMatch(
            match_id=str(event.get("id", "")),
            league_id=league_id,
            home=home_info.get("name", "Unknown"),
            away=away_info.get("name", "Unknown"),
            start_time=int(event.get("time", 0)),
        )


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
