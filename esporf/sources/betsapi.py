"""BetsAPI client for fetching eSoccer odds and match data.

BetsAPI is the primary data source. It tracks odds from Bet365, Pinnacle,
and other books for eSoccer GT Leagues, GG League, and Volta.

Docs: https://betsapi.com/docs/
Pricing: ~$10/month for basic access.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from esporf.config import settings
from esporf.models import (
    MarketType,
    Match,
    OddsLine,
    Outcome,
)

logger = logging.getLogger(__name__)

# BetsAPI sport ID for soccer (eSoccer is under soccer)
SPORT_ID = 1

# Map BetsAPI odds keys to our outcome model
_MONEYLINE_MAP = {
    "home": Outcome.HOME,
    "draw": Outcome.DRAW,
    "away": Outcome.AWAY,
}


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
        """Make an authenticated GET request to BetsAPI."""
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

    # ── Match fetching ───────────────────────────────────────────────

    async def get_upcoming_matches(self, league_id: int) -> list[Match]:
        """Fetch upcoming (pre-match) events for a league."""
        data = await self._request(
            "/events/upcoming",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        return [self._parse_match(ev, league_id) for ev in data.get("results", [])]

    async def get_inplay_matches(self, league_id: int) -> list[Match]:
        """Fetch live / in-play events for a league."""
        data = await self._request(
            "/events/inplay",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        matches = []
        for ev in data.get("results", []):
            m = self._parse_match(ev, league_id)
            m.is_live = True
            matches.append(m)
        return matches

    async def get_ended_matches(
        self, league_id: int, page: int = 1
    ) -> list[Match]:
        """Fetch recently ended events (for CLV and historical analysis)."""
        data = await self._request(
            "/events/ended",
            params={"sport_id": SPORT_ID, "league_id": league_id, "page": page},
        )
        return [self._parse_match(ev, league_id) for ev in data.get("results", [])]

    # ── Odds fetching ────────────────────────────────────────────────

    async def get_odds(self, match_id: str) -> list[OddsLine]:
        """Fetch all available odds for a match from the odds endpoint."""
        data = await self._request(
            "/event/odds",
            params={"event_id": match_id},
        )
        return self._parse_odds(data.get("results", {}))

    async def get_bet365_odds(self, match_id: str) -> list[OddsLine]:
        """Fetch Bet365-specific odds (often the sharpest for eSoccer)."""
        try:
            data = await self._request(
                "/bet365/prematch",
                params={"FI": match_id},
            )
            return self._parse_bet365_odds(data.get("results", []))
        except Exception as e:
            logger.warning("Failed to fetch Bet365 odds for %s: %s", match_id, e)
            return []

    async def enrich_match_with_odds(self, match: Match) -> Match:
        """Fetch odds from all available sources and attach to a match."""
        odds = await self.get_odds(match.match_id)
        b365_odds = await self.get_bet365_odds(match.match_id)
        match.odds = odds + b365_odds
        return match

    # ── Parsing helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_match(event: dict, league_id: int) -> Match:
        """Parse a BetsAPI event dict into our Match model."""
        home_info = event.get("home", {})
        away_info = event.get("away", {})
        scores = event.get("scores", {})

        return Match(
            match_id=str(event.get("id", "")),
            league_id=league_id,
            home=home_info.get("name", "Unknown"),
            away=away_info.get("name", "Unknown"),
            start_time=int(event.get("time", 0)),
            home_score=_safe_int(scores.get("2", {}).get("home")),
            away_score=_safe_int(scores.get("2", {}).get("away")),
        )

    @staticmethod
    def _parse_odds(results: dict) -> list[OddsLine]:
        """Parse the generic /event/odds response into OddsLine objects."""
        odds_lines: list[OddsLine] = []

        for book_key, book_data in results.items():
            book_name = book_data.get("name", book_key) if isinstance(book_data, dict) else book_key

            if not isinstance(book_data, dict):
                continue

            # 1X2 (moneyline)
            odds_1x2 = book_data.get("odds", {}).get("1_1")
            if odds_1x2 and isinstance(odds_1x2, dict):
                for key, outcome in _MONEYLINE_MAP.items():
                    val = _safe_float(odds_1x2.get(key))
                    if val and val > 1.0:
                        odds_lines.append(
                            OddsLine(
                                sportsbook=book_name,
                                market=MarketType.MONEYLINE,
                                outcome=outcome,
                                odds=val,
                            )
                        )

            # Over/Under totals
            odds_ou = book_data.get("odds", {}).get("1_2")
            if odds_ou and isinstance(odds_ou, dict):
                line_val = _safe_float(odds_ou.get("handicap"))
                over_val = _safe_float(odds_ou.get("over"))
                under_val = _safe_float(odds_ou.get("under"))
                if over_val and over_val > 1.0:
                    odds_lines.append(
                        OddsLine(
                            sportsbook=book_name,
                            market=MarketType.TOTAL,
                            outcome=Outcome.OVER,
                            odds=over_val,
                            line=line_val,
                        )
                    )
                if under_val and under_val > 1.0:
                    odds_lines.append(
                        OddsLine(
                            sportsbook=book_name,
                            market=MarketType.TOTAL,
                            outcome=Outcome.UNDER,
                            odds=under_val,
                            line=line_val,
                        )
                    )

            # Asian handicap / spread
            odds_ah = book_data.get("odds", {}).get("1_3")
            if odds_ah and isinstance(odds_ah, dict):
                line_val = _safe_float(odds_ah.get("handicap"))
                home_val = _safe_float(odds_ah.get("home"))
                away_val = _safe_float(odds_ah.get("away"))
                if home_val and home_val > 1.0:
                    odds_lines.append(
                        OddsLine(
                            sportsbook=book_name,
                            market=MarketType.SPREAD,
                            outcome=Outcome.HOME,
                            odds=home_val,
                            line=line_val,
                        )
                    )
                if away_val and away_val > 1.0:
                    odds_lines.append(
                        OddsLine(
                            sportsbook=book_name,
                            market=MarketType.SPREAD,
                            outcome=Outcome.AWAY,
                            odds=away_val,
                            line=line_val,
                        )
                    )

        return odds_lines

    @staticmethod
    def _parse_bet365_odds(results: list) -> list[OddsLine]:
        """Parse Bet365-specific prematch odds."""
        odds_lines: list[OddsLine] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            market_type = item.get("type")
            # Full-time result (1X2)
            if market_type == "1":
                for sel in item.get("selections", []):
                    name = sel.get("name", "").lower()
                    odds_val = _safe_float(sel.get("odds"))
                    if not odds_val or odds_val <= 1.0:
                        continue
                    outcome = None
                    if "home" in name or name == "1":
                        outcome = Outcome.HOME
                    elif "draw" in name or name == "x":
                        outcome = Outcome.DRAW
                    elif "away" in name or name == "2":
                        outcome = Outcome.AWAY
                    if outcome:
                        odds_lines.append(
                            OddsLine(
                                sportsbook="Bet365",
                                market=MarketType.MONEYLINE,
                                outcome=outcome,
                                odds=odds_val,
                            )
                        )
            # Goals over/under
            elif market_type == "5":
                line_val = _safe_float(item.get("handicap"))
                for sel in item.get("selections", []):
                    name = sel.get("name", "").lower()
                    odds_val = _safe_float(sel.get("odds"))
                    if not odds_val or odds_val <= 1.0:
                        continue
                    if "over" in name:
                        odds_lines.append(
                            OddsLine(
                                sportsbook="Bet365",
                                market=MarketType.TOTAL,
                                outcome=Outcome.OVER,
                                odds=odds_val,
                                line=line_val,
                            )
                        )
                    elif "under" in name:
                        odds_lines.append(
                            OddsLine(
                                sportsbook="Bet365",
                                market=MarketType.TOTAL,
                                outcome=Outcome.UNDER,
                                odds=odds_val,
                                line=line_val,
                            )
                        )
        return odds_lines


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
