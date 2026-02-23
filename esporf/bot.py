"""Main bot loop — polls for upcoming matches, analyzes trends, sends alerts.

Workflow:
1. On first run, backfill match history from BetsAPI into SQLite
2. Every cycle: fetch newly ended matches and add to DB
3. Fetch upcoming matches from AceOdds (full day) + ESportsBattle (~30 min) + BetsAPI
4. Fetch external stats from TotalCorner (per-player) + Forebet (predictions)
5. Run trend analysis on each matchup (with external data when available)
6. Display results and send webhook alerts for qualifying trends
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from esporf.sources.forebet import ForebetClient
from esporf.sources.totalcorner import TotalCornerClient

logger = logging.getLogger(__name__)
console = Console()


def _match_key(m: UpcomingMatch) -> str:
    """Normalize a match to a dedup key based on players + start time.

    Handles are lowercased so that AceOdds "GLORY" matches BetsAPI "Glory".
    """
    h = extract_handle(m.home).lower()
    a = extract_handle(m.away).lower()
    pair = tuple(sorted([h, a]))
    return f"{pair[0]}_{pair[1]}_{m.start_time}"


class EsporfBot:
    """The main bot that collects data, finds trends, and sends alerts."""

    def __init__(self):
        self.api = BetsAPIClient()
        self.esb = ESportsBattleClient()
        self.ace = AceOddsClient()
        self.tc = TotalCornerClient()
        self.forebet = ForebetClient()
        self.db = MatchDatabase()
        self.analyzer = TrendAnalyzer(self.db)
        self._running = False
        self._scan_count = 0
        self._alerted_keys: set[str] = set()

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

    async def _fetch_external_data(self) -> None:
        """Pre-fetch TotalCorner and Forebet data for all tracked leagues.

        Runs in parallel. Results are cached on each client instance.
        """
        tasks = []
        for lid in settings.tracked_league_ids:
            if TotalCornerClient.supports_league(lid):
                tasks.append(self.tc.get_league_stats(lid))
        tasks.append(self.forebet.get_predictions())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        tc_count = sum(
            1 for r in results
            if r is not None and not isinstance(r, (Exception, BaseException)) and hasattr(r, "players")
        )
        fb_result = results[-1] if results else None
        fb_count = len(fb_result) if isinstance(fb_result, list) else 0

        sources = []
        if tc_count:
            sources.append(f"TC: {tc_count} league(s)")
        if fb_count:
            sources.append(f"Forebet: {fb_count} pred(s)")
        if sources:
            console.print(f"  [dim]External: {', '.join(sources)}[/dim]")

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

        # Fetch external data (TotalCorner + Forebet) in parallel
        await self._fetch_external_data()

        # 1. Long-range: AceOdds (full day schedule, 7+ hours ahead)
        all_upcoming: list[UpcomingMatch] = []
        seen_keys: set[str] = set()
        key_to_idx: dict[str, int] = {}  # key → index in all_upcoming

        # Track matches confirmed as Volta by a dedicated Volta source.
        # BetsAPI misclassifies GG League / GT Leagues matches under Volta's
        # league_id, so we only trust matches that ESportsBattle (the Volta
        # tournament organizer) or AceOdds (bet365 Volta page) also list.
        volta_confirmed: set[str] = set()

        try:
            ace_matches = await self.ace.get_volta_schedule()
            for m in ace_matches:
                key = _match_key(m)
                volta_confirmed.add(key)
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
                volta_confirmed.add(key)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
        except Exception as e:
            logger.warning("ESportsBattle schedule failed: %s", e)

        # 3. Supplementary: BetsAPI (other leagues + real-time)
        for lid in settings.tracked_league_ids:
            try:
                schedule = await self.api.get_full_schedule(lid)
                for m in schedule:
                    key = _match_key(m)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        key_to_idx[key] = len(all_upcoming)
                        all_upcoming.append(m)
                    elif key in key_to_idx:
                        existing = all_upcoming[key_to_idx[key]]
                        if existing.match_id.startswith(("esb_", "ace_")):
                            existing.match_id = m.match_id
                            logger.debug(
                                "Cross-referenced %s vs %s with BetsAPI ID %s",
                                m.home, m.away, m.match_id,
                            )
            except Exception as e:
                logger.warning("BetsAPI schedule failed for league %d: %s", lid, e)

        # Fuzzy cross-reference pass: AceOdds timestamps come from "HH:MM"
        # text (always second-aligned to :00), while BetsAPI timestamps may
        # differ by up to a few minutes.  Exact key matching misses these,
        # leaving ace_ matches without a BetsAPI ID (and therefore no odds).
        # Match by player pair + approximate time (within 5 min) to fix this.
        still_unresolved = [
            (i, m) for i, m in enumerate(all_upcoming)
            if m.match_id.startswith(("esb_", "ace_"))
        ]
        if still_unresolved:
            betsapi_lookup: dict[tuple[str, str], list[tuple[int, UpcomingMatch]]] = {}
            for i, m in enumerate(all_upcoming):
                if m.match_id.startswith(("esb_", "ace_")):
                    continue
                h = extract_handle(m.home).lower()
                a = extract_handle(m.away).lower()
                pair = tuple(sorted([h, a]))
                betsapi_lookup.setdefault(pair, []).append((i, m))

            cross_ref_dupes: set[int] = set()
            for idx, m in still_unresolved:
                h = extract_handle(m.home).lower()
                a = extract_handle(m.away).lower()
                pair = tuple(sorted([h, a]))
                best_candidate = None
                best_delta = 301  # exceeds 300s threshold
                best_bets_idx = -1
                for bets_idx, candidate in betsapi_lookup.get(pair, []):
                    if bets_idx in cross_ref_dupes:
                        continue
                    delta = abs(candidate.start_time - m.start_time)
                    if delta <= 300 and delta < best_delta:
                        best_candidate = candidate
                        best_delta = delta
                        best_bets_idx = bets_idx
                if best_candidate is not None:
                    m.match_id = best_candidate.match_id
                    cross_ref_dupes.add(best_bets_idx)
                    logger.debug(
                        "Fuzzy cross-ref %s vs %s → BetsAPI %s (Δ%ds)",
                        m.home, m.away, best_candidate.match_id, best_delta,
                    )

            if cross_ref_dupes:
                all_upcoming = [
                    m for i, m in enumerate(all_upcoming)
                    if i not in cross_ref_dupes
                ]
                logger.info(
                    "Fuzzy cross-ref resolved %d match(es), removed %d duplicate(s)",
                    len(cross_ref_dupes), len(cross_ref_dupes),
                )

        # Drop matches that don't belong to a tracked league (BetsAPI can
        # return neighbouring-league events, e.g. GT Leagues under Volta)
        tracked = set(settings.tracked_league_ids)
        all_upcoming = [m for m in all_upcoming if m.league_id in tracked]

        # Strict Volta enforcement: BetsAPI misclassifies GG League and
        # GT Leagues matches under Volta's league_id (38439).  Only keep
        # matches that were also listed by a Volta-specific source
        # (ESportsBattle or AceOdds).  If both sources failed, skip this
        # filter to avoid a complete blackout.
        if volta_confirmed:
            before = len(all_upcoming)
            all_upcoming = [
                m for m in all_upcoming
                if _match_key(m) in volta_confirmed
            ]
            dropped = before - len(all_upcoming)
            if dropped:
                logger.info(
                    "Volta filter: dropped %d match(es) not confirmed by ESB/AceOdds",
                    dropped,
                )

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
        betsapi_matches = [
            m for m in all_upcoming
            if not m.match_id.startswith(("esb_", "ace_"))
        ]
        if betsapi_matches:
            await self.api.fetch_odds_batch(betsapi_matches)
        odds_count = sum(1 for m in all_upcoming if m.odds and m.odds.has_data)
        if odds_count:
            console.print(f"  [dim]Got odds for {odds_count}/{len(all_upcoming)} matches[/dim]")

        # Get Forebet predictions (already cached from _fetch_external_data)
        forebet_preds = await self.forebet.get_predictions()

        # Analyze each matchup for trends (with external data)
        reports: list[MatchupReport] = []
        for match in all_upcoming:
            # Get TotalCorner stats for this match's league
            tc_stats = await self.tc.get_league_stats(match.league_id)

            # Find matching Forebet prediction
            forebet_pred = self.forebet.match_prediction(
                match.home, match.away, forebet_preds
            ) if forebet_preds else None

            report = self.analyzer.analyze_matchup(
                match, tc_stats=tc_stats, forebet_pred=forebet_pred
            )
            reports.append(report)

        # Display results
        reports_with_picks = [r for r in reports if r.best_bet is not None]
        reports_trend_only = [
            r for r in reports
            if r.has_trends and r.best_bet is None and r.best_trend_pick is not None
        ]
        all_alertable = reports_with_picks + reports_trend_only

        display_scan_summary(
            total_matches=len(all_upcoming),
            matches_with_trends=len(all_alertable),
            total_trends=sum(len(r.trends) for r in all_alertable),
            db_total=self.db.total_matches(),
        )
        if reports_trend_only:
            console.print(
                f"  [dim]{len(reports_trend_only)} match(es) alerting on trends (no book odds yet)[/dim]"
            )

        for report in reports:
            display_matchup_report(report)

        # Send alerts for matches with real odds OR strong trend-only picks.
        # Track alerted matches to avoid duplicate alerts. When a trend-only
        # alert is sent first and real odds arrive later, the match won't be
        # re-alerted (the early heads-up is enough).
        # Uses the content-based _match_key (players + start_time) instead
        # of match_id, which can change between scans when BetsAPI
        # cross-references an ace_/esb_ match with a different numeric ID.
        new_reports = [
            r for r in all_alertable
            if _match_key(r.match) not in self._alerted_keys
        ]
        if new_reports:
            await send_alerts(new_reports)
            for r in new_reports:
                self._alerted_keys.add(_match_key(r.match))

        return all_alertable

    async def run(self) -> None:
        """Run the bot in a continuous polling loop."""
        self._running = True
        interval = settings.poll_interval

        console.print(
            f"[bold blue]Esporf Trend Bot Starting[/bold blue]\n"
            f"  Tracking leagues: {settings.league_ids}\n"
            f"  Poll interval: {interval}s\n"
            f"  Schedule: [bold green]AceOdds[/bold green] + ESportsBattle + BetsAPI\n"
            f"  External: [bold cyan]TotalCorner[/bold cyan] + Forebet\n"
            f"  Odds: BetsAPI v2 → odds/summary → bet365/prematch\n"
            f"  Min hit rate: {settings.min_hit_rate:.0%}\n"
            f"  Min sample size: {settings.min_sample_size}\n"
            f"  Goal lines: {settings.goal_lines} + book-offered\n"
            f"  Press Ctrl+C to stop\n"
        )

        # Backfill history on startup
        await self.backfill()

        # Set up signal handlers (add_signal_handler is not supported on Windows)
        if os.name != "nt":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown)

        # Each scan should complete well within the poll interval; if it
        # hangs (e.g. a DNS lookup stalls), cancel it so the loop continues.
        scan_timeout = max(interval * 2, 120)

        try:
            while self._running:
                try:
                    await asyncio.wait_for(self.scan_once(), timeout=scan_timeout)
                except asyncio.TimeoutError:
                    logger.error("Scan timed out after %ds — skipping", scan_timeout)
                    console.print(
                        f"[red]Scan timed out after {scan_timeout}s — will retry next cycle[/red]"
                    )
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
            await self.tc.close()
            await self.forebet.close()
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
        await bot.tc.close()
        await bot.forebet.close()
        bot.db.close()
