"""TotalCorner scraper — per-player eSoccer stats and Over X.5 hit rates.

TotalCorner publishes two key tables per league:
1. Player Stats: MP, W/D/L, GF, GA, Avg GF, Avg GA
2. Total Goals Over Rates: per-player Over 1.5 through Over 10.5 hit %

These feed into the Poisson model (better avg-goals estimates) and serve
as independent trend signals alongside our own match-history analysis.

Data is cached for 1 hour per league.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from esporf.models import extract_handle

logger = logging.getLogger(__name__)

# BetsAPI league ID → TotalCorner league ID
_TC_LEAGUE_MAP: dict[int, int] = {
    38439: 38895,  # Volta 6 min
    37298: 37552,  # H2H GG League 8 min
    23114: 12985,  # GT Leagues 12 min
}

_BASE_URL = "https://www.totalcorner.com/league/view"
_CACHE_TTL = 3600  # 1 hour

_OVER_RE = re.compile(r"over\s+(\d+\.5)", re.IGNORECASE)


@dataclass
class PlayerStats:
    """Per-player statistics from TotalCorner."""

    handle: str
    matches_played: int
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0
    avg_goals_scored: float = 0.0
    avg_goals_conceded: float = 0.0
    over_rates: dict[float, float] = field(default_factory=dict)  # line → 0.0-1.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.matches_played if self.matches_played > 0 else 0.0

    @property
    def avg_total_goals(self) -> float:
        return self.avg_goals_scored + self.avg_goals_conceded


@dataclass
class LeagueStats:
    """All player stats for one league."""

    tc_league_id: int
    players: dict[str, PlayerStats]  # handle (lowercase) → stats
    fetched_at: float = 0.0

    def lookup(self, player_name: str) -> PlayerStats | None:
        """Look up by player handle, case-insensitive."""
        handle = extract_handle(player_name).lower()
        return self.players.get(handle)


class TotalCornerClient:
    """Scrapes player stats from TotalCorner league pages."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[int, LeagueStats] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=20.0,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/121.0.0.0 Safari/537.36"
                    ),
                },
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @staticmethod
    def supports_league(betsapi_league_id: int) -> bool:
        return betsapi_league_id in _TC_LEAGUE_MAP

    async def get_league_stats(self, betsapi_league_id: int) -> LeagueStats | None:
        """Fetch player stats for a league. Returns cached data when fresh."""
        tc_id = _TC_LEAGUE_MAP.get(betsapi_league_id)
        if tc_id is None:
            return None

        now = time.time()
        cached = self._cache.get(tc_id)
        if cached and (now - cached.fetched_at) < _CACHE_TTL:
            logger.debug(
                "TotalCorner: cache hit league %d (%d players)", tc_id, len(cached.players)
            )
            return cached

        try:
            client = await self._get_client()
            resp = await client.get(f"{_BASE_URL}/{tc_id}")
            resp.raise_for_status()
            stats = self._parse_page(resp.text, tc_id)
            stats.fetched_at = now
            self._cache[tc_id] = stats
            logger.info("TotalCorner: %d players for league %d", len(stats.players), tc_id)
            return stats
        except Exception as e:
            logger.warning("TotalCorner fetch failed (league %d): %s", tc_id, e)
            return cached  # stale cache better than nothing

    def _parse_page(self, html: str, tc_league_id: int) -> LeagueStats:
        soup = BeautifulSoup(html, "lxml")
        players = self._parse_player_table(soup)
        over_rates = self._parse_over_table(soup)
        for handle, rates in over_rates.items():
            if handle in players:
                players[handle].over_rates = rates
        return LeagueStats(tc_league_id=tc_league_id, players=players)

    # ── Player Statistics table ───────────────────────────────────

    def _parse_player_table(self, soup: BeautifulSoup) -> dict[str, PlayerStats]:
        stats: dict[str, PlayerStats] = {}

        for table in soup.find_all("table"):
            header_row = table.find("tr")
            if not header_row:
                continue
            headers = [
                th.get_text(strip=True).lower().replace(".", "")
                for th in header_row.find_all(["th", "td"])
            ]
            hstr = " ".join(headers)
            if "avg gf" not in hstr or "win" not in hstr:
                continue

            idx: dict[str, int] = {}
            for i, h in enumerate(headers):
                if h in ("player", "team", "name"):
                    idx["player"] = i
                elif h in ("mp", "p", "played"):
                    idx["mp"] = i
                elif h == "win":
                    idx["win"] = i
                elif h == "draw":
                    idx["draw"] = i
                elif h == "lose":
                    idx["lose"] = i
                elif h == "gf":
                    idx["gf"] = i
                elif h == "ga":
                    idx["ga"] = i
                elif h in ("avg gf",):
                    idx["avg_gf"] = i
                elif h in ("avg ga",):
                    idx["avg_ga"] = i

            if "player" not in idx or "mp" not in idx:
                continue

            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                try:
                    link = cells[idx["player"]].find("a")
                    name = (link.get_text(strip=True) if link else
                            cells[idx["player"]].get_text(strip=True))
                    handle = name.lower().strip()
                    if not handle:
                        continue
                    mp = int(cells[idx["mp"]].get_text(strip=True))
                    if mp <= 0:
                        continue

                    def _int(key: str) -> int:
                        return int(cells[idx[key]].get_text(strip=True)) if key in idx else 0

                    def _float(key: str) -> float:
                        return float(cells[idx[key]].get_text(strip=True)) if key in idx else 0.0

                    stats[handle] = PlayerStats(
                        handle=handle,
                        matches_played=mp,
                        wins=_int("win"),
                        draws=_int("draw"),
                        losses=_int("lose"),
                        goals_for=_int("gf"),
                        goals_against=_int("ga"),
                        avg_goals_scored=_float("avg_gf"),
                        avg_goals_conceded=_float("avg_ga"),
                    )
                except (ValueError, IndexError):
                    continue
            if stats:
                break
        return stats

    # ── Total Goals Over rates table ──────────────────────────────

    def _parse_over_table(self, soup: BeautifulSoup) -> dict[str, dict[float, float]]:
        """Parse the 'Total Goals Statistics & Prediction' table.

        Returns {handle: {1.5: 1.0, 2.5: 0.94, 3.5: 0.86, ...}}
        """
        rates: dict[str, dict[float, float]] = {}

        for table in soup.find_all("table"):
            header_row = table.find("tr")
            if not header_row:
                continue
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
            hstr = " ".join(headers).lower()
            if "over 1.5" not in hstr and "over 2.5" not in hstr:
                continue

            player_idx: int | None = None
            over_cols: dict[int, float] = {}
            for i, h in enumerate(headers):
                hl = h.lower().strip()
                if hl in ("player", "team", "name"):
                    player_idx = i
                else:
                    m = _OVER_RE.match(hl)
                    if m:
                        over_cols[i] = float(m.group(1))

            if player_idx is None or not over_cols:
                continue

            for row in table.find_all("tr")[1:]:
                cells = row.find_all("td")
                try:
                    link = cells[player_idx].find("a")
                    name = (link.get_text(strip=True) if link else
                            cells[player_idx].get_text(strip=True))
                    handle = name.lower().strip()
                    if not handle:
                        continue
                    player_rates: dict[float, float] = {}
                    for ci, line_val in over_cols.items():
                        if ci < len(cells):
                            txt = cells[ci].get_text(strip=True).replace("%", "")
                            if txt:
                                player_rates[line_val] = float(txt) / 100.0
                    if player_rates:
                        rates[handle] = player_rates
                except (ValueError, IndexError):
                    continue
            if rates:
                break
        return rates
