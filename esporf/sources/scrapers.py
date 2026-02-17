"""Free web scrapers for supplemental eSoccer data.

These scrapers pull stats and results from freely-accessible websites
to supplement the paid BetsAPI odds data. Used for:
- Historical player/team stats (win rates, goals per match)
- Head-to-head records
- Over/under hit rates

Sources:
- Sofascore eSoccer section (live scores, results)
- TotalCorner (corners, cards, goals stats)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class PlayerStats:
    """Aggregated stats for an eSoccer player/team."""

    name: str
    matches_played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_scored: int = 0
    goals_conceded: int = 0
    avg_goals_scored: float = 0.0
    avg_goals_conceded: float = 0.0
    over_2_5_rate: float = 0.0  # % of matches going over 2.5 goals
    btts_rate: float = 0.0  # % of matches with both teams scoring
    win_rate: float = 0.0
    recent_form: list[str] = field(default_factory=list)  # e.g. ["W","L","W","W","D"]

    @property
    def avg_total_goals(self) -> float:
        return self.avg_goals_scored + self.avg_goals_conceded


@dataclass
class HeadToHead:
    """Head-to-head record between two players."""

    player_a: str
    player_b: str
    total_matches: int = 0
    player_a_wins: int = 0
    player_b_wins: int = 0
    draws: int = 0
    avg_total_goals: float = 0.0


class SofascoreScraper:
    """Scrape eSoccer data from Sofascore's public API endpoints.

    Sofascore exposes a JSON API under /api/v1/ that doesn't require
    authentication for basic match data.
    """

    BASE_URL = "https://api.sofascore.com/api/v1"

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers=_HEADERS,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_esoccer_events(self, date: str | None = None) -> list[dict]:
        """Get eSoccer events for a given date (YYYY-MM-DD).

        Sofascore categorizes eSoccer under football with specific
        tournament/category IDs. We search for tournaments containing
        'esoccer' or 'e-soccer' in the name.
        """
        client = await self._get_client()
        try:
            # Sofascore scheduled events endpoint for football
            url = f"{self.BASE_URL}/sport/football/scheduled-events/{date or 'today'}"
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

            esoccer_events = []
            for event in data.get("events", []):
                tournament = event.get("tournament", {})
                tournament_name = tournament.get("name", "").lower()
                category = tournament.get("category", {}).get("name", "").lower()

                if any(
                    kw in tournament_name
                    for kw in ["esoccer", "e-soccer", "volta", "gg league", "gt league"]
                ) or "esoccer" in category:
                    esoccer_events.append(event)

            logger.info("Found %d eSoccer events from Sofascore", len(esoccer_events))
            return esoccer_events
        except Exception as e:
            logger.warning("Sofascore scrape failed: %s", e)
            return []

    async def get_player_last_matches(
        self, player_id: int, count: int = 20
    ) -> list[dict]:
        """Get a player/team's last N matches from Sofascore."""
        client = await self._get_client()
        try:
            url = f"{self.BASE_URL}/team/{player_id}/events/last/0"
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", [])[:count]
            return events
        except Exception as e:
            logger.warning("Failed to get player matches from Sofascore: %s", e)
            return []


class TotalCornerScraper:
    """Scrape stats from TotalCorner.com for eSoccer statistical analysis.

    TotalCorner provides over/under stats, corner stats, and goal
    averages that are useful for building expected-value models.
    """

    BASE_URL = "https://www.totalcorner.com"

    # Known TotalCorner league IDs for eSoccer
    LEAGUE_MAP = {
        "gt_leagues": 12985,
        "gg_league": 37552,
    }

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers=_HEADERS,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_league_stats(self, league_key: str) -> list[PlayerStats]:
        """Scrape player/team statistics from a TotalCorner league page."""
        league_id = self.LEAGUE_MAP.get(league_key)
        if not league_id:
            logger.warning("Unknown TotalCorner league key: %s", league_key)
            return []

        client = await self._get_client()
        try:
            url = f"{self.BASE_URL}/league/view/{league_id}"
            resp = await client.get(url)
            resp.raise_for_status()
            return self._parse_league_page(resp.text)
        except Exception as e:
            logger.warning("TotalCorner scrape failed for %s: %s", league_key, e)
            return []

    async def get_match_stats(self, match_url: str) -> dict:
        """Scrape detailed stats for a specific match page on TotalCorner."""
        client = await self._get_client()
        try:
            resp = await client.get(match_url)
            resp.raise_for_status()
            return self._parse_match_page(resp.text)
        except Exception as e:
            logger.warning("TotalCorner match scrape failed: %s", e)
            return {}

    @staticmethod
    def _parse_league_page(html: str) -> list[PlayerStats]:
        """Parse player stats from a TotalCorner league overview page."""
        soup = BeautifulSoup(html, "lxml")
        players: list[PlayerStats] = []

        # TotalCorner league pages have a standings table with team stats
        table = soup.find("table", class_=re.compile(r"table.*standing|leagueTable"))
        if not table:
            tables = soup.find_all("table")
            # Pick the largest table as the likely standings table
            table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)

        if not table:
            logger.warning("Could not find standings table on TotalCorner page")
            return players

        rows = table.find_all("tr")[1:]  # skip header
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue
            try:
                name = cols[0].get_text(strip=True)
                # Typical columns: Team, P, W, D, L, GF, GA, ...
                played = _extract_int(cols[1].get_text(strip=True))
                wins = _extract_int(cols[2].get_text(strip=True))
                draws = _extract_int(cols[3].get_text(strip=True))
                losses = _extract_int(cols[4].get_text(strip=True))
                gf = _extract_int(cols[5].get_text(strip=True)) if len(cols) > 5 else 0
                ga = _extract_int(cols[6].get_text(strip=True)) if len(cols) > 6 else 0

                ps = PlayerStats(
                    name=name,
                    matches_played=played,
                    wins=wins,
                    draws=draws,
                    losses=losses,
                    goals_scored=gf,
                    goals_conceded=ga,
                )
                if played > 0:
                    ps.avg_goals_scored = gf / played
                    ps.avg_goals_conceded = ga / played
                    ps.win_rate = wins / played
                players.append(ps)
            except (ValueError, IndexError):
                continue

        return players

    @staticmethod
    def _parse_match_page(html: str) -> dict:
        """Parse individual match statistics from TotalCorner."""
        soup = BeautifulSoup(html, "lxml")
        stats: dict = {}

        stat_rows = soup.find_all("div", class_=re.compile(r"stat-row|matchStat"))
        for row in stat_rows:
            label_el = row.find(class_=re.compile(r"stat-label|name"))
            if not label_el:
                continue
            label = label_el.get_text(strip=True).lower()
            values = row.find_all(class_=re.compile(r"stat-value|value"))
            if len(values) >= 2:
                stats[label] = {
                    "home": values[0].get_text(strip=True),
                    "away": values[1].get_text(strip=True),
                }

        return stats


def _extract_int(text: str) -> int:
    """Extract first integer from a string."""
    match = re.search(r"\d+", text)
    return int(match.group()) if match else 0
