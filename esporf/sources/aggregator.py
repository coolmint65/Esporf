"""Aggregator that combines data from all sources into unified match objects.

This is the main entry point for getting enriched match data.
It fetches from BetsAPI (odds) and supplements with scraped stats.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from esporf.config import settings
from esporf.models import Match
from esporf.sources.betsapi import BetsAPIClient
from esporf.sources.scrapers import (
    PlayerStats,
    SofascoreScraper,
    TotalCornerScraper,
)

logger = logging.getLogger(__name__)


@dataclass
class EnrichedMatch:
    """A match with both odds data and supplemental statistics."""

    match: Match
    home_stats: PlayerStats | None = None
    away_stats: PlayerStats | None = None
    extra: dict = field(default_factory=dict)


class DataAggregator:
    """Combines all data sources into a unified view of eSoccer matches."""

    def __init__(self):
        self.betsapi = BetsAPIClient()
        self.sofascore = SofascoreScraper()
        self.totalcorner = TotalCornerScraper()
        self._stats_cache: dict[str, PlayerStats] = {}

    async def close(self) -> None:
        await asyncio.gather(
            self.betsapi.close(),
            self.sofascore.close(),
            self.totalcorner.close(),
            return_exceptions=True,
        )

    async def get_all_matches(self) -> list[EnrichedMatch]:
        """Fetch all upcoming and live matches across tracked leagues."""
        league_ids = settings.tracked_league_ids

        # Fetch upcoming + inplay for all leagues concurrently
        tasks = []
        for lid in league_ids:
            tasks.append(self.betsapi.get_upcoming_matches(lid))
            tasks.append(self.betsapi.get_inplay_matches(lid))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_matches: list[Match] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Failed to fetch matches: %s", result)
                continue
            all_matches.extend(result)

        # Deduplicate by match_id
        seen: set[str] = set()
        unique: list[Match] = []
        for m in all_matches:
            if m.match_id not in seen:
                seen.add(m.match_id)
                unique.append(m)

        logger.info("Found %d unique matches across %d leagues", len(unique), len(league_ids))

        # Enrich with odds (batch, with concurrency limit)
        enriched = await self._enrich_batch(unique)
        return enriched

    async def get_ended_matches(self, league_id: int, pages: int = 1) -> list[Match]:
        """Fetch recently ended matches for historical analysis."""
        all_ended: list[Match] = []
        for page in range(1, pages + 1):
            try:
                matches = await self.betsapi.get_ended_matches(league_id, page=page)
                all_ended.extend(matches)
            except Exception as e:
                logger.warning("Failed to fetch ended matches page %d: %s", page, e)
                break
        return all_ended

    async def refresh_stats_cache(self) -> None:
        """Refresh player statistics from TotalCorner."""
        for league_key in ["gt_leagues", "gg_league"]:
            try:
                stats = await self.totalcorner.get_league_stats(league_key)
                for ps in stats:
                    self._stats_cache[ps.name.lower()] = ps
                logger.info(
                    "Cached %d player stats from TotalCorner (%s)", len(stats), league_key
                )
            except Exception as e:
                logger.warning("Failed to refresh stats for %s: %s", league_key, e)

    async def _enrich_batch(
        self, matches: list[Match], max_concurrent: int = 5
    ) -> list[EnrichedMatch]:
        """Enrich a batch of matches with odds and stats, with concurrency limit."""
        semaphore = asyncio.Semaphore(max_concurrent)
        enriched: list[EnrichedMatch] = []

        async def enrich_one(match: Match) -> EnrichedMatch:
            async with semaphore:
                try:
                    await self.betsapi.enrich_match_with_odds(match)
                except Exception as e:
                    logger.warning("Failed to get odds for %s: %s", match.display_name, e)

                home_stats = self._stats_cache.get(match.home.lower())
                away_stats = self._stats_cache.get(match.away.lower())

                return EnrichedMatch(
                    match=match,
                    home_stats=home_stats,
                    away_stats=away_stats,
                )

        tasks = [enrich_one(m) for m in matches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, EnrichedMatch):
                enriched.append(r)
            else:
                logger.warning("Enrichment failed: %s", r)

        return enriched
