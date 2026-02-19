"""AceOdds scraper for fetching the full-day Volta match schedule.

AceOdds publishes bet365's complete eSoccer Battle Volta schedule as a
static HTML table.  This gives us a **7+ hour lookahead** with exact
matchups — far beyond ESportsBattle's ~30 minute window.

The page is server-rendered (no JS), making it simple to parse.
Times are in UTC.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from esporf.models import UpcomingMatch

logger = logging.getLogger(__name__)

_SCHEDULE_URL = (
    "https://www.aceodds.com/bet365-live-streaming/football/"
    "esoccer-battle-volta-6-mins-play.html"
)

# BetsAPI league ID for Volta (used when creating UpcomingMatch objects)
_VOLTA_LEAGUE_ID = 38439

# Pattern: "Team (Player) v Team (Player)"
_MATCH_RE = re.compile(r"(.+?)\s*\((.+?)\)\s*v\s*(.+?)\s*\((.+?)\)")


# Refresh the schedule at most every 12 hours
_CACHE_TTL = 12 * 3600


class AceOddsClient:
    """Scrapes the AceOdds Volta schedule page.

    Results are cached so we only hit the site once or twice a day.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[UpcomingMatch] = []
        self._cache_time: float = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Accept": "text/html",
                    "User-Agent": "Mozilla/5.0 (compatible; Esporf/1.0)",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_volta_schedule(self) -> list[UpcomingMatch]:
        """Return the full-day Volta schedule, using cache when fresh.

        Only fetches from AceOdds if the cache is older than 12 hours.
        Returns UpcomingMatch objects with UTC-based unix timestamps.
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < _CACHE_TTL:
            logger.debug("AceOdds: using cached schedule (%d matches)", len(self._cache))
            return self._cache

        client = await self._get_client()
        resp = await client.get(_SCHEDULE_URL)
        resp.raise_for_status()
        html = resp.text

        self._cache = self._parse_schedule(html)
        self._cache_time = now
        return self._cache

    def _parse_schedule(self, html: str) -> list[UpcomingMatch]:
        """Parse match entries from the AceOdds HTML table."""
        soup = BeautifulSoup(html, "lxml")
        table = soup.select_one("div.table-responsive-sm table")
        if not table:
            logger.warning("AceOdds: could not find schedule table")
            return []

        now_utc = datetime.now(tz=timezone.utc)
        matches: list[UpcomingMatch] = []

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            # Skip header rows (th-based or colspan date headers)
            if not cells or len(cells) != 2 or cells[0].get("colspan"):
                continue

            time_str = cells[0].get_text(strip=True)
            link = cells[1].find("a")
            if not link:
                continue

            match_text = link.get_text(strip=True)
            m = _MATCH_RE.match(match_text)
            if not m:
                continue

            home_team, home_player, away_team, away_player = m.groups()
            home_team = home_team.strip()
            away_team = away_team.strip()
            home_player = home_player.strip()
            away_player = away_player.strip()

            # Parse time (HH:MM in UTC) into a unix timestamp
            start_time = self._parse_utc_time(time_str, now_utc)
            if start_time is None:
                continue

            # Format names to match BetsAPI/sportsbook style: "Team (Handle)"
            home = f"{home_team} ({home_player})"
            away = f"{away_team} ({away_player})"

            # Use a deterministic match ID based on content
            match_id = f"ace_{start_time}_{home_player}_{away_player}"

            matches.append(
                UpcomingMatch(
                    match_id=match_id,
                    league_id=_VOLTA_LEAGUE_ID,
                    home=home,
                    away=away,
                    start_time=start_time,
                )
            )

        if matches:
            first_min = max(0, (matches[0].start_time - int(now_utc.timestamp())) // 60)
            last_min = max(0, (matches[-1].start_time - int(now_utc.timestamp())) // 60)
            logger.info(
                "AceOdds: %d Volta matches (%d min to %d min ahead)",
                len(matches), first_min, last_min,
            )

        return matches

    @staticmethod
    def _parse_utc_time(time_str: str, now_utc: datetime) -> int | None:
        """Convert 'HH:MM' UTC string to a unix timestamp for today/tomorrow."""
        try:
            parts = time_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1])
        except (ValueError, IndexError):
            return None

        # Build a datetime for today at this time in UTC
        dt = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If this time is more than 2 hours in the past, it's probably tomorrow
        if dt.timestamp() < now_utc.timestamp() - 7200:
            from datetime import timedelta
            dt = dt + timedelta(days=1)

        return int(dt.timestamp())
