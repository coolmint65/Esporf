"""Main bot loop that polls for matches, detects edges, and sends alerts.

This is the core scheduler that runs continuously:
1. Fetches all upcoming/live eSoccer matches across tracked leagues
2. Enriches them with odds from multiple sportsbooks
3. Runs edge detection (line discrepancy, steam moves, CLV, model)
4. Outputs results to console and sends webhook alerts
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from esporf.alerts.console import display_edges, display_scan_summary
from esporf.alerts.webhooks import send_alerts
from esporf.analysis.edge_detector import EdgeDetector
from esporf.config import settings
from esporf.sources.aggregator import DataAggregator

logger = logging.getLogger(__name__)
console = Console()


class EsporfBot:
    """The main bot that orchestrates data collection and edge detection."""

    def __init__(self):
        self.aggregator = DataAggregator()
        self.detector = EdgeDetector()
        self._running = False
        self._scan_count = 0
        self._total_edges_found = 0

    async def scan_once(self) -> int:
        """Run a single scan cycle. Returns number of edges found."""
        self._scan_count += 1
        timestamp = datetime.now().strftime("%H:%M:%S")
        console.print(f"\n[dim]── Scan #{self._scan_count} at {timestamp} ──[/dim]")

        # Refresh stats cache periodically (every 10 scans)
        if self._scan_count % 10 == 1:
            with console.status("Refreshing player stats..."):
                await self.aggregator.refresh_stats_cache()

        # Fetch all matches with odds
        with console.status("Fetching matches and odds..."):
            enriched_matches = await self.aggregator.get_all_matches()

        if not enriched_matches:
            console.print("[yellow]No matches found. Will retry next cycle.[/yellow]")
            return 0

        # Run edge detection
        edges = self.detector.analyze_all(enriched_matches)

        # Display results
        display_scan_summary(
            total_matches=len(enriched_matches),
            total_edges=len(edges),
            leagues_scanned=len(settings.tracked_league_ids),
        )

        if edges:
            display_edges(edges)
            self._total_edges_found += len(edges)

            # Send webhook alerts
            await send_alerts(edges)

        return len(edges)

    async def run(self) -> None:
        """Run the bot in a continuous polling loop."""
        self._running = True
        interval = settings.poll_interval

        console.print(
            f"[bold blue]Esporf Bot Starting[/bold blue]\n"
            f"  Tracking leagues: {settings.league_ids}\n"
            f"  Poll interval: {interval}s\n"
            f"  Min edge: {settings.min_edge_percent}%\n"
            f"  Press Ctrl+C to stop\n"
        )

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown)

        try:
            while self._running:
                try:
                    await self.scan_once()
                except Exception as e:
                    logger.error("Scan failed: %s", e, exc_info=True)
                    console.print(f"[red]Scan error: {e}[/red]")

                if self._running:
                    console.print(
                        f"[dim]Next scan in {interval}s... "
                        f"(total edges found: {self._total_edges_found})[/dim]"
                    )
                    await asyncio.sleep(interval)
        finally:
            await self.aggregator.close()
            console.print("\n[bold]Bot stopped.[/bold]")

    def _shutdown(self) -> None:
        """Handle graceful shutdown."""
        console.print("\n[yellow]Shutting down...[/yellow]")
        self._running = False


async def run_bot() -> None:
    """Entry point to start the bot."""
    bot = EsporfBot()
    await bot.run()


async def scan_once() -> None:
    """Run a single scan (useful for testing/cron)."""
    bot = EsporfBot()
    try:
        await bot.scan_once()
    finally:
        await bot.aggregator.close()
