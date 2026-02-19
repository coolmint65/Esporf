"""Main bot loop — polls for upcoming matches, analyzes trends, sends alerts.

Workflow:
1. On first run, backfill match history from BetsAPI into SQLite
2. Every cycle: fetch newly ended matches and add to DB
3. Fetch upcoming/live matches
4. Run trend analysis on each matchup
5. Display results and send webhook alerts for qualifying trends
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime

from rich.console import Console

from esporf.alerts.console import display_matchup_report, display_scan_summary
from esporf.alerts.webhooks import send_alerts
from esporf.analysis.trends import TrendAnalyzer
from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import MatchupReport
from esporf.sources.betsapi import BetsAPIClient

logger = logging.getLogger(__name__)
console = Console()


class EsporfBot:
    """The main bot that collects data, finds trends, and sends alerts."""

    def __init__(self):
        self.api = BetsAPIClient()
        self.db = MatchDatabase()
        self.analyzer = TrendAnalyzer(self.db)
        self._running = False
        self._scan_count = 0
        self._alerted_match_ids: set[str] = set()

    async def backfill(self) -> None:
        """Fetch historical match data to populate the database on first run."""
        if self.db.total_matches() > 0:
            console.print(
                f"[dim]Database already has {self.db.total_matches():,} matches. "
                f"Fetching latest results...[/dim]"
            )
            # Just grab page 1 of ended matches to stay current
            for lid in settings.tracked_league_ids:
                try:
                    matches = await self.api.get_ended_matches(lid, page=1)
                    added = self.db.insert_many(matches)
                    if added:
                        logger.info("Added %d new results for league %d", added, lid)
                except Exception as e:
                    logger.warning("Failed to update league %d: %s", lid, e)
            return

        # First run — full backfill
        console.print("[bold]First run — backfilling match history...[/bold]")
        pages = settings.backfill_pages
        for lid in settings.tracked_league_ids:
            console.print(f"  Fetching league {lid} ({pages} pages)...")
            try:
                matches = await self.api.backfill_history(lid, pages=pages)
                added = self.db.insert_many(matches)
                console.print(f"    Added {added} matches for league {lid}")
            except Exception as e:
                console.print(f"    [red]Failed: {e}[/red]")

        console.print(
            f"[bold green]Backfill complete: {self.db.total_matches():,} matches in DB[/bold green]\n"
        )

    async def scan_once(self) -> list[MatchupReport]:
        """Run a single scan cycle. Returns reports with qualifying trends."""
        self._scan_count += 1
        timestamp = datetime.now().strftime("%H:%M:%S")
        console.print(f"\n[dim]── Scan #{self._scan_count} at {timestamp} ──[/dim]")

        # Update DB with latest ended matches
        for lid in settings.tracked_league_ids:
            try:
                ended = await self.api.get_ended_matches(lid, page=1)
                added = self.db.insert_many(ended)
                if added:
                    logger.info("Added %d new results for league %d", added, lid)
            except Exception as e:
                logger.warning("Failed to fetch ended matches for league %d: %s", lid, e)

        # Fetch upcoming matches (only those starting within 1 hour)
        all_upcoming = []
        for lid in settings.tracked_league_ids:
            try:
                upcoming = await self.api.get_upcoming_matches(lid)
                inplay = await self.api.get_inplay_matches(lid)
                all_upcoming.extend(upcoming)
                all_upcoming.extend(inplay)
            except Exception as e:
                logger.warning("Failed to fetch upcoming for league %d: %s", lid, e)

        # Only keep matches starting within 1 hour (or already live)
        imminent = [m for m in all_upcoming if m.starts_within(3600)]

        if not imminent:
            skipped = len(all_upcoming)
            msg = (
                f"[yellow]No matches within the next hour."
                f"{f' ({skipped} later matches skipped.)' if skipped else ''}"
                f" Will retry next cycle.[/yellow]"
            )
            console.print(msg)
            return []

        all_upcoming = imminent

        # Analyze each matchup for trends
        reports: list[MatchupReport] = []
        for match in all_upcoming:
            report = self.analyzer.analyze_matchup(match)
            reports.append(report)

        # Display results
        reports_with_trends = [r for r in reports if r.has_trends]
        total_trends = sum(len(r.trends) for r in reports_with_trends)

        display_scan_summary(
            total_matches=len(all_upcoming),
            matches_with_trends=len(reports_with_trends),
            total_trends=total_trends,
            db_total=self.db.total_matches(),
        )

        for report in reports:
            display_matchup_report(report)

        # Send alerts for NEW matchups only (avoid spamming same matchup)
        new_reports = [
            r for r in reports_with_trends
            if r.match.match_id not in self._alerted_match_ids
        ]
        if new_reports:
            await send_alerts(new_reports)
            for r in new_reports:
                self._alerted_match_ids.add(r.match.match_id)

        return reports_with_trends

    async def run(self) -> None:
        """Run the bot in a continuous polling loop."""
        self._running = True
        interval = settings.poll_interval

        console.print(
            f"[bold blue]Esporf Trend Bot Starting[/bold blue]\n"
            f"  Tracking leagues: {settings.league_ids}\n"
            f"  Poll interval: {interval}s\n"
            f"  Min hit rate: {settings.min_hit_rate:.0%}\n"
            f"  Min sample size: {settings.min_sample_size}\n"
            f"  Goal lines: {settings.goal_lines}\n"
            f"  Press Ctrl+C to stop\n"
        )

        # Backfill history on startup
        await self.backfill()

        # Set up signal handlers
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
                    console.print(f"[dim]Next scan in {interval}s...[/dim]")
                    await asyncio.sleep(interval)
        finally:
            await self.api.close()
            self.db.close()
            console.print("\n[bold]Bot stopped.[/bold]")

    def _shutdown(self) -> None:
        console.print("\n[yellow]Shutting down...[/yellow]")
        self._running = False


async def run_bot() -> None:
    bot = EsporfBot()
    await bot.run()


async def scan_once() -> None:
    bot = EsporfBot()
    try:
        await bot.backfill()
        await bot.scan_once()
    finally:
        await bot.api.close()
        bot.db.close()
