"""Main bot loop — polls for upcoming matches, finds odds-backed picks, sends alerts.

Workflow:
1. On first run, backfill match history from BetsAPI into SQLite
2. Every cycle: fetch newly ended matches and add to DB
3. Fetch upcoming matches from AceOdds (full day) + ESportsBattle (~30 min) + Kambi + BetsAPI
4. Fetch external stats from TotalCorner (per-player) + Forebet (predictions)
5. Fetch real sportsbook odds from BetsAPI (bet365) + Kambi (pre-live)
6. Run trend analysis and only surface picks with real odds + positive edge
7. Send webhook alerts for qualifying odds-backed picks only
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime

from rich.console import Console

from esporf.alerts.console import display_matchup_report, display_scan_summary
from esporf.alerts.webhooks import send_alerts
from esporf.analysis.trends import TrendAnalyzer
from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import (
    BetPick,
    MatchResult,
    MatchupReport,
    PickResult,
    TrackedPick,
    UpcomingMatch,
    extract_handle,
    _parse_line,
    _parse_spread,
)
from esporf.sources.aceodds import AceOddsClient
from esporf.sources.betsapi import BetsAPIClient
from esporf.sources.esportsbattle import ESportsBattleClient
from esporf.sources.forebet import ForebetClient
from esporf.sources.kambi import KambiClient
from esporf.sources.totalcorner import TotalCornerClient

logger = logging.getLogger(__name__)
console = Console()


def _match_key(m: UpcomingMatch) -> str:
    """Normalize a match to a dedup key based on players + start time.

    Handles are lowercased so that AceOdds "GLORY" matches BetsAPI "Glory".
    Start time is rounded to the nearest 10-minute mark so that the same
    match from different sources (which can report times up to ~5 min
    apart) produces the same key.  Without rounding, a 1-second difference
    in start_time creates entirely different keys, letting duplicates
    through every dedup layer — schedule building, alert sending, and
    pick recording.
    """
    h = extract_handle(m.home).lower()
    a = extract_handle(m.away).lower()
    pair = tuple(sorted([h, a]))
    rounded_time = round(m.start_time / 600) * 600
    return f"{pair[0]}_{pair[1]}_{rounded_time}"


class EsporfBot:
    """The main bot that collects data, finds trends, and sends alerts."""

    def __init__(self, *, skip_webhook_alerts: bool = False):
        self.api = BetsAPIClient()
        self.esb = ESportsBattleClient()
        self.ace = AceOddsClient()
        self.kambi = KambiClient()
        self.tc = TotalCornerClient()
        self.forebet = ForebetClient()
        self.db = MatchDatabase()
        self.analyzer = TrendAnalyzer(self.db)
        self._running = False
        self._scan_count = 0
        self._alerted_keys: set[str] = set()
        self._skip_webhook_alerts = skip_webhook_alerts
        self._load_alerted_keys()

    # ── Pick tracking ────────────────────────────────────────────

    def _load_alerted_keys(self) -> None:
        """Populate the alerted set from DB so restarts don't duplicate picks.

        Loads picks created in the last 24 hours and builds _match_key-style
        keys so the scan loop treats them as already alerted.
        """
        since = int(time.time()) - 86400  # last 24h
        rows = self.db.get_recent_pick_matches(since)
        for home, away, start_time in rows:
            h = extract_handle(home).lower()
            a = extract_handle(away).lower()
            pair = tuple(sorted([h, a]))
            rounded_time = round(start_time / 600) * 600
            key = f"{pair[0]}_{pair[1]}_{rounded_time}"
            self._alerted_keys.add(key)
        if self._alerted_keys:
            logger.info(
                "Loaded %d alerted key(s) from DB to prevent duplicates",
                len(self._alerted_keys),
            )

    def record_pick(self, report: MatchupReport) -> TrackedPick | None:
        """Record an alerted pick in the database for W/L tracking."""
        pick = report.best_bet
        if not pick:
            return None

        tracked = TrackedPick(
            match_id=report.match.match_id,
            league_id=report.match.league_id,
            home=report.match.home,
            away=report.match.away,
            start_time=report.match.start_time,
            market=pick.market,
            units=pick.units,
            odds=pick.decimal_odds,
            hit_rate=max(t.hit_rate for t in pick.supporting_trends),
            edge=pick.edge,
            created_at=int(time.time()),
        )
        row_id = self.db.insert_pick(tracked)
        if row_id is None:
            logger.debug("Pick already exists: %s %s", report.match.display_name, pick.market)
            return None
        tracked.id = row_id
        logger.info("Recorded pick #%d: %s %s", row_id, report.match.display_name, pick.market)
        return tracked

    def resolve_pending_picks(self) -> list[TrackedPick]:
        """Check pending picks against completed match results and resolve them."""
        pending = self.db.get_pending_picks()
        if not pending:
            return []

        resolved: list[TrackedPick] = []
        now = int(time.time())

        for pick in pending:
            # Look up the match result by trying to find it in the DB
            result_match = self._find_match_result(pick)
            if result_match is None:
                continue

            outcome, profit = self._evaluate_pick(pick, result_match)
            self.db.resolve_pick(
                pick_id=pick.id,
                result=outcome,
                profit=profit,
                home_score=result_match.home_score,
                away_score=result_match.away_score,
                resolved_at=now,
            )

            pick.result = outcome
            pick.profit = profit
            pick.home_score = result_match.home_score
            pick.away_score = result_match.away_score
            pick.resolved_at = now
            resolved.append(pick)

            emoji = pick.result_emoji
            logger.info(
                "%s Pick #%d resolved: %s %s → %s (%s)",
                emoji, pick.id, extract_handle(pick.home),
                f"vs {extract_handle(pick.away)}", outcome.value,
                pick.profit_display,
            )

        if resolved:
            console.print(
                f"  [dim]Resolved {len(resolved)} pick(s): "
                f"{sum(1 for p in resolved if p.result == PickResult.WIN)}W "
                f"{sum(1 for p in resolved if p.result == PickResult.LOSS)}L "
                f"{sum(1 for p in resolved if p.result == PickResult.PUSH)}P[/dim]"
            )

        return resolved

    def _find_match_result(self, pick: TrackedPick) -> MatchResult | None:
        """Find the completed match result for a tracked pick."""
        # 1. Direct match_id lookup (fastest, most reliable)
        conn = self.db._get_conn()
        row = conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (pick.match_id,)
        ).fetchone()
        if row:
            return self.db._row_to_match(row)

        # 2. H2H by full player names + start_time tolerance.
        #    Must pass the original names (e.g. "Bayer 04 (Sheva)") so that
        #    _handle_pattern can build the correct %(Handle) LIKE pattern.
        #    The old code extracted handles first, which produced bare strings
        #    like "Sheva" that _handle_pattern treated as exact matches — never
        #    matching "Bayer 04 (Sheva)" in the DB.
        h2h = self.db.get_h2h_matches(pick.home, pick.away, limit=10)
        for match in h2h:
            if abs(match.start_time - pick.start_time) <= 300:
                return match
        return None

    @staticmethod
    def _evaluate_pick(
        pick: TrackedPick, result: MatchResult
    ) -> tuple[PickResult, float]:
        """Determine if a pick won or lost and calculate profit.

        Returns (outcome, profit_in_units).
        """
        market = pick.market
        total_goals = result.total_goals

        parsed_line = _parse_line(market)
        parsed_spread = _parse_spread(market)

        won = False

        if parsed_line:
            direction, line = parsed_line
            if direction.lower() == "over":
                won = total_goals > line
            else:
                won = total_goals < line
        elif parsed_spread:
            player_name, handicap = parsed_spread
            # Determine which side the pick is on
            pick_handle = player_name.lower()
            home_handle = extract_handle(pick.home).lower()
            away_handle = extract_handle(pick.away).lower()

            if home_handle in pick_handle or pick_handle in home_handle:
                goal_diff = result.home_score - result.away_score
            elif away_handle in pick_handle or pick_handle in away_handle:
                goal_diff = result.away_score - result.home_score
            else:
                # Can't determine side — skip
                return PickResult.PUSH, 0.0

            adjusted = goal_diff + handicap
            if adjusted > 0:
                won = True
            elif adjusted == 0:
                return PickResult.PUSH, 0.0
            else:
                won = False
        elif "win" in market.lower():
            # Moneyline win market — extract player name
            market_lower = market.lower()
            home_handle = extract_handle(pick.home).lower()
            away_handle = extract_handle(pick.away).lower()

            if home_handle in market_lower or pick.home.lower() in market_lower:
                won = result.home_score > result.away_score
            elif away_handle in market_lower or pick.away.lower() in market_lower:
                won = result.away_score > result.home_score
            else:
                return PickResult.PUSH, 0.0

            # Draw is a loss for win markets
            if result.is_draw:
                won = False
        else:
            # Unknown market type — can't evaluate
            return PickResult.PUSH, 0.0

        if won:
            payout = (pick.odds - 1.0) * pick.units if pick.odds else pick.units
            return PickResult.WIN, round(payout, 2)
        else:
            return PickResult.LOSS, round(-pick.units, 2)

    async def backfill(self) -> None:
        """Fetch historical match data to populate the database on first run.

        Also does a full backfill for any tracked league that has zero matches
        in the DB (e.g. newly added 2025 season leagues).
        """
        pages = settings.backfill_pages

        if self.db.total_matches() == 0:
            # First run — full backfill for all leagues
            console.print("[bold]First run — backfilling match history...[/bold]")
            for lid in settings.tracked_league_ids:
                console.print(f"  Fetching league {lid} ({pages} pages)...")
                try:
                    matches = await self.api.backfill_history(lid, pages=pages)
                    added = self.db.insert_many(matches)
                    console.print(f"    Added {added} matches for league {lid}")
                except Exception as e:
                    console.print(f"    [red]Failed: {e}[/red]")

            # Build player form stats from the fresh history
            form_count = self.db.rebuild_player_form(settings.tracked_league_ids)
            console.print(f"  [dim]Built form profiles for {form_count} players[/dim]")

            console.print(
                f"[bold green]Backfill complete: {self.db.total_matches():,} matches in DB[/bold green]\n"
            )
            return

        console.print(
            f"[dim]Database already has {self.db.total_matches():,} matches. "
            f"Checking for updates...[/dim]"
        )

        for lid in settings.tracked_league_ids:
            league_count = self.db.total_matches_for_league(lid)
            if league_count == 0:
                # New league with no history — full backfill
                console.print(
                    f"  [bold]New league {lid} — backfilling {pages} pages...[/bold]"
                )
                try:
                    matches = await self.api.backfill_history(lid, pages=pages)
                    added = self.db.insert_many(matches)
                    console.print(f"    Added {added} matches for league {lid}")
                except Exception as e:
                    console.print(f"    [red]Failed: {e}[/red]")
            else:
                # Existing league — just grab latest
                try:
                    matches = await self.api.get_ended_matches(lid, page=1)
                    added = self.db.insert_many(matches)
                    if added:
                        logger.info("Added %d new results for league %d", added, lid)
                except Exception as e:
                    logger.warning("Failed to update league %d: %s", lid, e)

        # Rebuild player form stats with any new data
        form_count = self.db.rebuild_player_form(settings.tracked_league_ids)
        if form_count:
            console.print(f"  [dim]Updated form profiles for {form_count} players[/dim]")

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
        scan_started = int(time.time())
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

        # Refresh player form stats with the new results
        self.db.rebuild_player_form(settings.tracked_league_ids)

        # Resolve any pending picks now that we have fresh results
        self.resolve_pending_picks()

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

        # 3. Kambi: schedule + odds in one shot (GG League, GT Leagues)
        kambi_league_ids: set[int] = set()
        try:
            kambi_matches = await self.kambi.get_schedule(settings.tracked_league_ids)
            for m in kambi_matches:
                key = _match_key(m)
                kambi_league_ids.add(m.league_id)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
        except Exception as e:
            logger.warning("Kambi schedule failed: %s", e)

        # 4. Supplementary: BetsAPI (leagues not covered by Kambi)
        for lid in settings.tracked_league_ids:
            if lid in kambi_league_ids:
                continue  # Kambi already provided schedule + odds for this league
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
        # Volta-league matches that were also listed by a Volta-specific
        # source (ESportsBattle or AceOdds).  Non-Volta leagues (GG League,
        # GT Leagues) pass through unfiltered.  If both Volta sources
        # failed, skip this filter to avoid a complete blackout.
        volta_lid = 38439
        if volta_confirmed:
            before = len(all_upcoming)
            all_upcoming = [
                m for m in all_upcoming
                if m.league_id != volta_lid or _match_key(m) in volta_confirmed
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

        # Last-chance cross-reference: some matches may still have ace_/esb_
        # IDs because BetsAPI hadn't listed them during schedule building.
        # Volta matches appear on BetsAPI very late (~2 min before kickoff),
        # so a quick re-check here can catch matches that just appeared.
        still_unresolved = [
            m for m in all_upcoming
            if m.match_id.startswith(("esb_", "ace_"))
        ]
        if still_unresolved:
            console.print(
                f"  [dim]{len(still_unresolved)} match(es) without BetsAPI ID "
                f"— trying last-minute lookup...[/dim]"
            )
            try:
                fresh: list[UpcomingMatch] = []
                # Only query BetsAPI for leagues not covered by Kambi
                for lid in settings.tracked_league_ids:
                    if lid in kambi_league_ids:
                        continue
                    fresh.extend(await self.api.get_inplay_matches(lid))
                    fresh.extend(await self.api.get_upcoming_matches(lid))

                # Build lookup by player pair
                fresh_lookup: dict[tuple[str, str], list[UpcomingMatch]] = {}
                for fm in fresh:
                    h = extract_handle(fm.home).lower()
                    a = extract_handle(fm.away).lower()
                    pair = tuple(sorted([h, a]))
                    fresh_lookup.setdefault(pair, []).append(fm)

                resolved = 0
                for m in still_unresolved:
                    h = extract_handle(m.home).lower()
                    a = extract_handle(m.away).lower()
                    pair = tuple(sorted([h, a]))
                    for candidate in fresh_lookup.get(pair, []):
                        if abs(candidate.start_time - m.start_time) <= 300:
                            m.match_id = candidate.match_id
                            resolved += 1
                            logger.info(
                                "Late cross-ref %s vs %s → BetsAPI %s",
                                m.home, m.away, candidate.match_id,
                            )
                            break
                if resolved:
                    console.print(
                        f"  [dim]Resolved {resolved}/{len(still_unresolved)} "
                        f"match(es) via late BetsAPI lookup[/dim]"
                    )
            except Exception as e:
                logger.warning("Late BetsAPI cross-ref failed: %s", e)

        # Fetch real odds — BetsAPI first, then Kambi for anything still missing
        # Skip kambi_ matches: they already have odds from get_schedule()
        betsapi_matches = [
            m for m in all_upcoming
            if not m.match_id.startswith(("esb_", "ace_", "kambi_"))
        ]
        if betsapi_matches:
            await self.api.fetch_odds_batch(betsapi_matches)

        # Kambi fills remaining gaps (Volta matches that BetsAPI missed)
        kambi_fallback = 0
        try:
            kambi_fallback = await self.kambi.attach_odds(all_upcoming)
        except Exception as e:
            logger.warning("kambi odds fetch failed: %s", e)

        odds_count = sum(1 for m in all_upcoming if m.odds and m.odds.has_data)
        if odds_count:
            sources = []
            # Kambi schedule matches + Kambi fallback matches
            kambi_total = sum(
                1 for m in all_upcoming
                if m.odds and m.odds.has_data and m.match_id.startswith("kambi_")
            ) + kambi_fallback
            betsapi_odds = odds_count - kambi_total
            if betsapi_odds > 0:
                sources.append(f"BetsAPI: {betsapi_odds}")
            if kambi_total > 0:
                sources.append(f"Kambi: {kambi_total}")
            console.print(
                f"  [dim]Got odds for {odds_count}/{len(all_upcoming)} "
                f"matches ({', '.join(sources)})[/dim]"
            )
        else:
            no_id = sum(
                1 for m in all_upcoming
                if m.match_id.startswith(("esb_", "ace_"))
            )
            console.print(
                f"  [yellow]No odds for any of {len(all_upcoming)} match(es). "
                f"{no_id} still without BetsAPI ID, "
                f"kambi attached {kambi_fallback}.[/yellow]"
            )

        # Persist odds snapshot for historical tracking
        snap_count = self.db.snapshot_odds(all_upcoming)
        if snap_count:
            logger.info("Stored %d odds snapshot rows", snap_count)

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

        # Display results — only picks backed by real sportsbook odds
        reports_with_picks = [r for r in reports if r.best_bet is not None]

        display_scan_summary(
            total_matches=len(all_upcoming),
            matches_with_picks=len(reports_with_picks),
            total_trends=sum(len(r.trends) for r in reports_with_picks),
            db_total=self.db.total_matches(),
        )

        for report in reports:
            display_matchup_report(report)

        # Send alerts only for picks backed by real sportsbook odds.
        # Uses the content-based _match_key (players + start_time) instead
        # of match_id, which can change between scans when BetsAPI
        # cross-references an ace_/esb_ match with a different numeric ID.
        # When running under the Discord bot, webhook alerts are skipped —
        # the bot handles delivery via its own channel.send().
        alerted_count = 0
        if not self._skip_webhook_alerts:
            new_reports = [
                r for r in reports_with_picks
                if _match_key(r.match) not in self._alerted_keys
            ]
            if new_reports:
                await send_alerts(new_reports)
                for r in new_reports:
                    self._alerted_keys.add(_match_key(r.match))
                    self.record_pick(r)
                alerted_count = len(new_reports)

        # Log scan metadata
        self.db.log_scan(
            scan_number=self._scan_count,
            started_at=scan_started,
            finished_at=int(time.time()),
            matches_found=len(all_upcoming),
            matches_with_odds=odds_count,
            picks_generated=len(reports_with_picks),
            picks_alerted=alerted_count,
            leagues_scanned=settings.league_ids,
        )

        return reports_with_picks

    async def run(self) -> None:
        """Run the bot in a continuous polling loop."""
        self._running = True
        interval = settings.poll_interval

        # Warn if the poll interval is suspiciously high — likely a stale .env
        if interval > 300:
            console.print(
                f"[bold yellow]Warning: POLL_INTERVAL={interval}s (from .env or env var). "
                f"Recommended: 180s. Update POLL_INTERVAL in your .env file.[/bold yellow]\n"
            )

        console.print(
            f"[bold blue]Esporf Odds Bot Starting[/bold blue]\n"
            f"  Tracking leagues: {settings.league_ids}\n"
            f"  Poll interval: {interval}s\n"
            f"  Schedule: [bold green]AceOdds[/bold green] + ESportsBattle + [bold cyan]Kambi[/bold cyan] + BetsAPI\n"
            f"  External: [bold cyan]TotalCorner[/bold cyan] + Forebet\n"
            f"  Odds: BetsAPI (bet365) + [bold cyan]Kambi[/bold cyan] (pre-live)\n"
            f"  Mode: [bold green]Sportsbook odds only[/bold green] (no trend-only picks)\n"
            f"  Min hit rate: {settings.min_hit_rate:.0%}\n"
            f"  Min sample size: {settings.min_sample_size}\n"
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
                    await self._interruptible_sleep(interval)
        finally:
            await self.api.close()
            await self.esb.close()
            await self.ace.close()
            await self.kambi.close()
            await self.tc.close()
            await self.forebet.close()
            self.db.close()
            console.print("\n[bold]Bot stopped.[/bold]")

    async def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep with an in-place countdown that updates every 5 seconds.

        Uses raw stdout with carriage return so the line updates in place
        on all terminals including Windows CMD.
        """
        for remaining in range(seconds, 0, -1):
            if not self._running:
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.flush()
                return
            # Update the countdown every 5 seconds (and on first tick)
            if remaining == seconds or remaining % 5 == 0:
                sys.stdout.write(f"\rNext scan in {remaining}s...   ")
                sys.stdout.flush()
            await asyncio.sleep(1)
        # Clear the countdown line when done
        sys.stdout.write("\r" + " " * 40 + "\r")
        sys.stdout.flush()

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
        await bot.kambi.close()
        await bot.tc.close()
        await bot.forebet.close()
        bot.db.close()
