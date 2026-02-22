"""Main bot loop — polls for upcoming matches, analyzes trends, sends alerts.

Workflow:
1. On first run, backfill match history from BetsAPI into SQLite
2. Every cycle: fetch newly ended matches and add to DB
3. Fetch upcoming matches from AceOdds (full day) + ESportsBattle (~30 min) + BetsAPI
4. Run trend analysis on each matchup
5. Display results and send webhook alerts for qualifying trends
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime

from rich.console import Console

from esporf.alerts.console import display_matchup_report, display_scan_summary
from esporf.alerts.webhooks import send_alerts
from esporf.analysis.trends import TrendAnalyzer
from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import MatchupReport, UpcomingMatch, extract_handle
from esporf.sources.aceodds import AceOddsClient
from esporf.sources.betsapi import BetsAPIClient
from esporf.sources.esportsbattle import ESportsBattleClient

logger = logging.getLogger(__name__)
console = Console()


def _match_key(m: UpcomingMatch) -> str:
    """Normalize a match to a dedup key based on players + start time."""
    h = extract_handle(m.home)
    a = extract_handle(m.away)
    pair = tuple(sorted([h, a]))
    return f"{pair[0]}_{pair[1]}_{m.start_time}"


class EsporfBot:
    """The main bot that collects data, finds trends, and sends alerts."""

    def __init__(self):
        self.api = BetsAPIClient()
        self.esb = ESportsBattleClient()
        self.ace = AceOddsClient()
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
        """Run a single scan cycle. Returns reports with qualifying trends.

        Uses three schedule sources in priority order:
        1. AceOdds — full day schedule (7+ hours ahead)
        2. ESportsBattle — ~30 min lookahead (exact matchups, confirms timing)
        3. BetsAPI — real-time + other leagues

        Fetches real odds for BetsAPI-sourced matches.
        """
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

        # 1. Long-range: AceOdds (full day schedule, 7+ hours ahead)
        all_upcoming: list[UpcomingMatch] = []
        seen_keys: set[str] = set()
        key_to_idx: dict[str, int] = {}  # key → index in all_upcoming

        try:
            ace_matches = await self.ace.get_volta_schedule()
            for m in ace_matches:
                key = _match_key(m)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
        except Exception as e:
            logger.warning("AceOdds schedule failed: %s", e)

        # 2. Near-term: ESportsBattle (~30 min lookahead, confirms timing)
        try:
            esb_matches = await self.esb.get_volta_schedule()
            for m in esb_matches:
                key = _match_key(m)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
        except Exception as e:
            logger.warning("ESportsBattle schedule failed: %s", e)

        # 3. Supplementary: BetsAPI (other leagues + real-time)
        # When a BetsAPI match duplicates an AceOdds/ESB match, swap in the
        # BetsAPI match_id so we can fetch real odds for it later.
        for lid in settings.tracked_league_ids:
            try:
                schedule = await self.api.get_full_schedule(lid)
                for m in schedule:
                    key = _match_key(m)
                    if key not in seen_keys:
                        # Brand new match only from BetsAPI
                        seen_keys.add(key)
                        key_to_idx[key] = len(all_upcoming)
                        all_upcoming.append(m)
                    elif key in key_to_idx:
                        # Match already exists from AceOdds/ESB — cross-reference
                        # the BetsAPI match_id so we can fetch real odds
                        existing = all_upcoming[key_to_idx[key]]
                        if existing.match_id.startswith(("esb_", "ace_")):
                            existing.match_id = m.match_id
                            logger.debug(
                                "Cross-referenced %s vs %s with BetsAPI ID %s",
                                m.home, m.away, m.match_id,
                            )
            except Exception as e:
                logger.warning("BetsAPI schedule failed for league %d: %s", lid, e)

        # Keep matches starting within lookahead window
        lookahead = settings.schedule_lookahead
        imminent = [m for m in all_upcoming if m.starts_within(lookahead)]

        if not imminent:
            skipped = len(all_upcoming)
            window = f"{lookahead // 60} min" if lookahead < 3600 else f"{lookahead // 3600}h"
            msg = (
                f"[yellow]No matches within the next {window}."
                f"{f' ({skipped} later/live matches skipped.)' if skipped else ''}"
                f" Will retry next cycle.[/yellow]"
            )
            console.print(msg)
            return []

        all_upcoming = imminent
        window = f"{lookahead // 60} min" if lookahead < 3600 else f"{lookahead // 3600}h"
        console.print(
            f"  [dim]Found {len(all_upcoming)} match(es) in next "
            f"{window} — fetching odds...[/dim]"
        )

        # Fetch real odds for BetsAPI-sourced matches only
        # (AceOdds/ESportsBattle IDs won't work with BetsAPI's odds endpoint)
        betsapi_matches = [
            m for m in all_upcoming
            if not m.match_id.startswith(("esb_", "ace_"))
        ]
        if betsapi_matches:
            await self.api.fetch_odds_batch(betsapi_matches)
        odds_count = sum(1 for m in all_upcoming if m.odds and m.odds.has_data)
        if odds_count:
            console.print(f"  [dim]Got odds for {odds_count}/{len(all_upcoming)} matches[/dim]")

        # Analyze each matchup for trends
        reports: list[MatchupReport] = []
        for match in all_upcoming:
            report = self.analyzer.analyze_matchup(match)
            reports.append(report)

        # Display results
        reports_with_picks = [r for r in reports if r.best_bet is not None]
        reports_no_odds = [
            r for r in reports
            if r.has_trends and r.best_bet is None
        ]

        display_scan_summary(
            total_matches=len(all_upcoming),
            matches_with_trends=len(reports_with_picks),
            total_trends=sum(len(r.trends) for r in reports_with_picks),
            db_total=self.db.total_matches(),
        )
        if reports_no_odds:
            console.print(
                f"  [dim]{len(reports_no_odds)} match(es) have trends but no book odds[/dim]"
            )

        for report in reports:
            display_matchup_report(report)

        # Send webhook alerts only for matches with real picks (actual odds)
        new_reports = [
            r for r in reports_with_picks
            if r.match.match_id not in self._alerted_match_ids
        ]
        if new_reports:
            await send_alerts(new_reports)
            for r in new_reports:
                self._alerted_match_ids.add(r.match.match_id)

        return reports_with_picks

    async def run(self) -> None:
        """Run the bot in a continuous polling loop."""
        self._running = True
        interval = settings.poll_interval

        lookahead_h = settings.schedule_lookahead // 3600
        console.print(
            f"[bold blue]Esporf Trend Bot Starting[/bold blue]\n"
            f"  Tracking leagues: {settings.league_ids}\n"
            f"  Poll interval: {interval}s\n"
            f"  Schedule: [bold green]AceOdds[/bold green] + ESportsBattle + BetsAPI\n"
            f"  Odds: BetsAPI v2\n"
            f"  Min hit rate: {settings.min_hit_rate:.0%}\n"
            f"  Min sample size: {settings.min_sample_size}\n"
            f"  Goal lines: {settings.goal_lines} + book-offered\n"
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
            await self.esb.close()
            await self.ace.close()
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
        await bot.esb.close()
        await bot.ace.close()
        bot.db.close()
