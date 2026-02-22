"""Forebet scraper — match-level eSoccer predictions.

Forebet publishes 1X2 probabilities and predicted scores for eSoccer.
Used as a cross-check signal: when Forebet's prediction aligns with our
own trend analysis, confidence is boosted.

NOTE: Forebet aggressively blocks automated requests (403). The scraper
attempts the fetch but degrades gracefully to empty results. This is
structured so it auto-activates if/when access becomes available.

Data is cached for 30 minutes.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from esporf.models import extract_handle

logger = logging.getLogger(__name__)

_PREDICTIONS_URL = "https://www.forebet.com/en/esoccer/predictions-for-today"
_CACHE_TTL = 1800  # 30 minutes


@dataclass
class ForebetPrediction:
    """A single match prediction from Forebet."""

    home: str
    away: str
    home_win_prob: float  # 0.0 - 1.0
    draw_prob: float
    away_win_prob: float
    predicted_home_goals: int = 0
    predicted_away_goals: int = 0
    avg_goals: float = 0.0

    @property
    def predicted_total(self) -> int:
        return self.predicted_home_goals + self.predicted_away_goals


class ForebetClient:
    """Scrapes eSoccer predictions from Forebet."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[ForebetPrediction] = []
        self._cache_time: float = 0
        self._available: bool = True  # False after first 403

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=15.0,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.google.com/",
                },
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_predictions(self) -> list[ForebetPrediction]:
        """Fetch today's eSoccer predictions. Returns [] if blocked/failed."""
        if not self._available:
            return self._cache

        now = time.time()
        if self._cache and (now - self._cache_time) < _CACHE_TTL:
            return self._cache

        try:
            client = await self._get_client()
            resp = await client.get(_PREDICTIONS_URL)
            if resp.status_code == 403:
                logger.info("Forebet: blocked (403) — disabling for this session")
                self._available = False
                return self._cache
            resp.raise_for_status()
            self._cache = self._parse_predictions(resp.text)
            self._cache_time = now
            logger.info("Forebet: parsed %d predictions", len(self._cache))
            return self._cache
        except Exception as e:
            logger.warning("Forebet fetch failed: %s", e)
            return self._cache

    def match_prediction(
        self, home: str, away: str, predictions: list[ForebetPrediction]
    ) -> ForebetPrediction | None:
        """Find the prediction matching a given matchup by player handles."""
        home_h = extract_handle(home).lower()
        away_h = extract_handle(away).lower()
        for p in predictions:
            ph = extract_handle(p.home).lower()
            pa = extract_handle(p.away).lower()
            if (ph == home_h and pa == away_h) or (ph == away_h and pa == home_h):
                return p
        return None

    def _parse_predictions(self, html: str) -> list[ForebetPrediction]:
        soup = BeautifulSoup(html, "lxml")
        predictions: list[ForebetPrediction] = []

        # Forebet uses div-based rows with class "rcnt" for each prediction
        for row in soup.select(".rcnt, tr.tr_0, tr.tr_1"):
            try:
                # Try multiple known Forebet structures
                home_el = row.select_one(".homeTeam, .tnms span:first-child, td:nth-child(1)")
                away_el = row.select_one(".awayTeam, .tnms span:last-child, td:nth-child(3)")
                if not home_el or not away_el:
                    continue

                home = home_el.get_text(strip=True)
                away = away_el.get_text(strip=True)
                if not home or not away:
                    continue

                # Probabilities (1X2)
                prob_els = row.select(".fprc span, .prob span, td.prob")
                probs = []
                for el in prob_els:
                    txt = el.get_text(strip=True).replace("%", "")
                    if txt.isdigit():
                        probs.append(int(txt) / 100.0)

                if len(probs) < 3:
                    continue

                # Predicted score
                score_el = row.select_one(".predict, .ex_sc, .foremark")
                pred_h, pred_a = 0, 0
                if score_el:
                    sm = re.search(r"(\d+)\s*[-:]\s*(\d+)", score_el.get_text(strip=True))
                    if sm:
                        pred_h, pred_a = int(sm.group(1)), int(sm.group(2))

                # Average goals
                avg_el = row.select_one(".avg_sc, .avgSc")
                avg_goals = float(pred_h + pred_a)
                if avg_el:
                    try:
                        avg_goals = float(avg_el.get_text(strip=True))
                    except ValueError:
                        pass

                predictions.append(ForebetPrediction(
                    home=home,
                    away=away,
                    home_win_prob=probs[0],
                    draw_prob=probs[1],
                    away_win_prob=probs[2],
                    predicted_home_goals=pred_h,
                    predicted_away_goals=pred_a,
                    avg_goals=avg_goals,
                ))
            except (ValueError, IndexError, AttributeError):
                continue

        return predictions
