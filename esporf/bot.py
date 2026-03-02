"""Main bot loop — polls for upcoming matches, finds odds-backed picks, records them.

Workflow:
1. On first run, backfill match history from BetsAPI into SQLite
2. Every cycle: fetch newly ended matches and add to DB
3. Fetch upcoming matches from AceOdds (full day) + ESportsBattle (~30 min) + HUDstats (GG League) + Kambi + BetsAPI
4. Fetch external stats from TotalCorner (per-player) + Forebet (predictions)
5. Fetch real sportsbook odds from BetsAPI (bet365) + Kambi (pre-live) + bwin (Volta) + FanDuel
6. Run trend analysis and only surface picks with real odds + positive edge
7. Record qualifying picks to DB (Discord alert delivery handled by discord_bot.py)
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

from esporf.alerts.console import display_matchup_report, display_reports_by_league, display_scan_summary
from esporf.analysis.trends import TrendAnalyzer
from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import (
    BetPick,
    MatchResult,
    MatchupReport,
    NO_TOTALS_LEAGUES,
    PickResult,
    TrackedPick,
    UpcomingMatch,
    extract_handle,
    extract_team,
    _parse_line,
    _parse_spread,
)
from esporf.sources.aceodds import AceOddsClient
from esporf.sources.betsapi import BetsAPIClient
from esporf.sources.bwin import BwinClient
from esporf.sources.esportsbattle import ESportsBattleClient
from esporf.sources.fanduel import FanDuelClient
from esporf.sources.forebet import ForebetClient
from esporf.sources.hudstats import HUDstatsClient, LiveScore
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

    def __init__(self):
        self.api = BetsAPIClient()
        self.esb = ESportsBattleClient()
        self.ace = AceOddsClient()
        self.kambi = KambiClient()
        self.hudstats = HUDstatsClient()
        self.bwin = BwinClient()
        self.fanduel = FanDuelClient()
        self.tc = TotalCornerClient()
        self.forebet = ForebetClient()
        self.db = MatchDatabase()
        self.analyzer = TrendAnalyzer(self.db)
        self._running = False
        self._scan_count = 0
        self._alerted_keys: set[str] = set()
        self._prev_hudstats_live: dict[str, LiveScore] = {}
        self._load_alerted_keys()

    # ── Name enrichment ──────────────────────────────────────────

    def _enrich_team_names(self, matches: list[UpcomingMatch]) -> None:
        """Fill in team names for matches that only have bare handles.

        When AceOdds/ESportsBattle fail, BetsAPI provides Volta matches
        with just "Senya" instead of "Germany (Senya)".  This looks up
        each player's most recent team from the DB and patches the name.
        """
        # Collect handles that need enrichment
        bare_handles: set[str] = set()
        for m in matches:
            if extract_team(m.home) is None:
                bare_handles.add(extract_handle(m.home).lower())
            if extract_team(m.away) is None:
                bare_handles.add(extract_handle(m.away).lower())

        if not bare_handles:
            return

        # Look up most recent "Team (Handle)" name for each bare handle
        team_cache = self.db.get_latest_team_names(bare_handles)

        enriched = 0
        for m in matches:
            if extract_team(m.home) is None:
                full_name = team_cache.get(extract_handle(m.home).lower())
                if full_name:
                    m.home = full_name
                    enriched += 1
            if extract_team(m.away) is None:
                full_name = team_cache.get(extract_handle(m.away).lower())
                if full_name:
                    m.away = full_name
                    enriched += 1

        if enriched:
            logger.info("Enriched %d bare handle(s) with team names from DB", enriched)

    # ── HUDstats score harvesting ─────────────────────────────────

    def _harvest_hudstats_results(self) -> int:
        """Detect finished GG League matches by comparing live score snapshots.

        Each scan cycle, HUDstats returns currently-live matches with scores.
        When a match that was live in the previous cycle disappears from the
        current live feed, it has finished.  We create a MatchResult from the
        last known scores and insert it into the DB so the pick resolver can
        grade it normally.

        Returns the number of results harvested.
        """
        current = self.hudstats.live_scores
        prev = self._prev_hudstats_live
        self._prev_hudstats_live = dict(current)  # snapshot for next cycle

        if not prev:
            return 0  # first cycle — nothing to compare against

        harvested = 0
        for match_id, score in prev.items():
            if match_id in current:
                continue  # still live — update will happen next cycle

            # Match was live last cycle but is gone now → finished
            result = MatchResult(
                match_id=match_id,
                league_id=score.league_id,
                home=score.home,
                away=score.away,
                home_score=score.home_score,
                away_score=score.away_score,
                start_time=score.start_time,
            )
            added = self.db.insert_many([result])
            if added:
                harvested += 1
                logger.info(
                    "Harvested GG League result: %s %d-%d %s [%s]",
                    score.home, score.home_score, score.away_score,
                    score.away, match_id,
                )

        return harvested

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

        # Guard: reject O/U picks for lines not in the match's actual odds.
        # Different odds sources can merge lines from different books, leading
        # to phantom picks like "Over 2.5" when the primary book starts at 5.5.
        odds = report.match.odds
        parsed = _parse_line(pick.market)
        if parsed and odds and odds.has_data:
            direction, line = parsed
            if not odds.get_line(line):
                logger.warning(
                    "Rejected pick — line %.1f not in current odds (%s): %s %s",
                    line, odds.available_lines,
                    report.match.display_name, pick.market,
                )
                return None

        # Resolve decimal odds — fall back to ML side lookup if property
        # returns None (can happen when _ml_side isn't set on the BetPick).
        dec_odds = pick.decimal_odds
        if dec_odds is None and pick.moneyline and report.match.odds:
            from esporf.models import _get_moneyline_dec_odds
            dec_odds = _get_moneyline_dec_odds(
                pick.market, pick.moneyline, report.match,
            )

        tracked = TrackedPick(
            match_id=report.match.match_id,
            league_id=report.match.league_id,
            home=report.match.home,
            away=report.match.away,
            start_time=report.match.start_time,
            market=pick.market,
            units=pick.units,
            odds=dec_odds,
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

    async def resolve_pending_picks(self) -> list[TrackedPick]:
        """Check pending picks against completed match results and resolve them.

        Three-pass approach:
        1. Fast pass — look up each pick in the local DB (direct match_id
           then H2H name+time).
        2. Targeted pass — for stale picks (match should have ended by now):
           a) Picks with numeric BetsAPI IDs: directly query the event by ID
              via ``/v1/event/view`` for an immediate, reliable result.
           b) All stale picks: fetch 2 pages of recently ended matches per
              league and re-attempt the DB lookup.
        3. Void pass — picks stuck for >3 hours get voided (result never
           coming back).
        """
        pending = self.db.get_pending_picks()
        if not pending:
            return []

        resolved: list[TrackedPick] = []
        now = int(time.time())
        still_pending: list[TrackedPick] = []

        # ── Pass 1: local DB lookup ──────────────────────────────────
        for pick in pending:
            result_match = self._find_match_result(pick)
            if result_match is not None:
                self._resolve_one(pick, result_match, now, resolved)
            else:
                still_pending.append(pick)

        # ── Pass 2: targeted lookups for stale picks ─────────────────
        # eSoccer matches last 8-12 min; 20 min buffer is generous.
        _STALE_THRESHOLD = 20 * 60  # seconds
        stale = [p for p in still_pending if (now - p.start_time) > _STALE_THRESHOLD]

        if stale:
            # 2a. Direct event view for picks with numeric BetsAPI IDs —
            #     most reliable, one API call per pick, gets the exact result.
            direct_resolved: set[int] = set()
            for pick in stale:
                if pick.match_id.startswith(("esb_", "ace_", "hudstats_", "kambi_", "bwin_")):
                    continue  # non-BetsAPI ID, skip direct lookup
                try:
                    result = await self.api.get_event_result(pick.match_id)
                    if result is not None:
                        self.db.insert_many([result])
                        self._resolve_one(pick, result, now, resolved)
                        direct_resolved.add(pick.id)
                        logger.info(
                            "Direct event view resolved pick #%d: %s",
                            pick.id, pick.match_id,
                        )
                except Exception as e:
                    logger.debug(
                        "Direct event view failed for pick #%d (%s): %s",
                        pick.id, pick.match_id, e,
                    )

            # Remove directly resolved picks from the stale list
            stale = [p for p in stale if p.id not in direct_resolved]

            # 2b. Bulk fetch ended matches (3 pages) for remaining stale picks
            if stale:
                stale_leagues = {p.league_id for p in stale}
                for lid in stale_leagues:
                    for page in (1, 2, 3):
                        try:
                            ended = await self.api.get_ended_matches(lid, page=page)
                            added = self.db.insert_many(ended)
                            if added:
                                logger.info(
                                    "Stale-pick resolver: added %d ended results "
                                    "for league %d (page %d)",
                                    added, lid, page,
                                )
                        except Exception as e:
                            logger.warning(
                                "Stale-pick resolver: failed to fetch ended "
                                "for league %d page %d: %s",
                                lid, page, e,
                            )

                # Re-attempt resolution with the freshly-inserted results
                for pick in stale:
                    result_match = self._find_match_result(pick)
                    if result_match is not None:
                        self._resolve_one(pick, result_match, now, resolved)
                    else:
                        age_min = (now - pick.start_time) // 60
                        logger.warning(
                            "Pick #%d still unresolved (%d min old): %s vs %s [%s]",
                            pick.id, age_min,
                            extract_handle(pick.home), extract_handle(pick.away),
                            pick.match_id,
                        )

        # ── Pass 3: void ancient pending picks ────────────────────
        # eSoccer matches last 8-12 min. If a pick is still pending
        # after 3 hours the result is never coming back — void it so
        # it doesn't sit in the live-picks list forever.
        _VOID_THRESHOLD = 3 * 3600  # 3 hours
        voided = 0
        for pick in self.db.get_pending_picks():
            if (now - pick.start_time) > _VOID_THRESHOLD:
                self.db.resolve_pick(
                    pick_id=pick.id,
                    result=PickResult.VOID,
                    profit=0.0,
                    home_score=pick.home_score or 0,
                    away_score=pick.away_score or 0,
                    resolved_at=now,
                )
                voided += 1
                logger.info(
                    "Voided stale pick #%d (%.1fh old): %s vs %s",
                    pick.id, (now - pick.start_time) / 3600,
                    extract_handle(pick.home), extract_handle(pick.away),
                )
        if voided:
            console.print(
                f"  [dim]Voided {voided} stale pick(s) (>3h old)[/dim]"
            )

        if resolved:
            console.print(
                f"  [dim]Resolved {len(resolved)} pick(s): "
                f"{sum(1 for p in resolved if p.result == PickResult.WIN)}W "
                f"{sum(1 for p in resolved if p.result == PickResult.LOSS)}L "
                f"{sum(1 for p in resolved if p.result == PickResult.PUSH)}P[/dim]"
            )

        return resolved

    def _resolve_one(
        self, pick: TrackedPick, result: MatchResult,
        now: int, resolved: list[TrackedPick],
    ) -> None:
        """Evaluate a single pick against its match result and persist."""
        outcome, profit = self._evaluate_pick(pick, result)
        self.db.resolve_pick(
            pick_id=pick.id,
            result=outcome,
            profit=profit,
            home_score=result.home_score,
            away_score=result.away_score,
            resolved_at=now,
        )

        pick.result = outcome
        pick.profit = profit
        pick.home_score = result.home_score
        pick.away_score = result.away_score
        pick.resolved_at = now
        resolved.append(pick)

        emoji = pick.result_emoji
        logger.info(
            "%s Pick #%d resolved: %s %s → %s (%s)",
            emoji, pick.id, extract_handle(pick.home),
            f"vs {extract_handle(pick.away)}", outcome.value,
            pick.profit_display,
        )

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
        #    _handle_patterns builds both bare and parenthesized LIKE patterns
        #    so lookups work regardless of name format differences between
        #    sources (e.g. Kambi bare "ALPHA" vs BetsAPI "Chelsea (ALPHA)").
        #    600s tolerance (up from 300s) because HUDstats, Kambi, and BetsAPI
        #    can report start times several minutes apart for the same match.
        h2h = self.db.get_h2h_matches(pick.home, pick.away, limit=10)
        for match in h2h:
            if abs(match.start_time - pick.start_time) <= 600:
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
            if not pick.odds:
                # No odds recorded — can't calculate real profit.
                # Treat as a push to avoid inflating P/L numbers.
                return PickResult.PUSH, 0.0
            payout = (pick.odds - 1.0) * pick.units
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

        # Harvest finished GG League matches from live score snapshots
        # BEFORE resolving picks — ensures freshly-finished matches are
        # available as MatchResults for the pick resolver this cycle.
        harvested = self._harvest_hudstats_results()
        if harvested:
            console.print(
                f"  [dim]Harvested {harvested} GG League result(s) from HUDstats[/dim]"
            )

        # Refresh player form stats with the new results
        self.db.rebuild_player_form(settings.tracked_league_ids)

        # Resolve any pending picks now that we have fresh results
        await self.resolve_pending_picks()

        # Fetch external data (TotalCorner + Forebet) in parallel
        await self._fetch_external_data()

        # 1. Long-range: AceOdds (full day schedule, 7+ hours ahead)
        all_upcoming: list[UpcomingMatch] = []
        seen_keys: set[str] = set()
        key_to_idx: dict[str, int] = {}  # key → index in all_upcoming

        # Track matches confirmed as Volta by a dedicated Volta source.
        # BetsAPI misclassifies GG League / GT Leagues matches under Volta's
        # league_id, so we only trust matches that ESportsBattle, AceOdds,
        # or bwin (which has dedicated Volta competitions) also list.
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

        # 2. Near-term: ESportsBattle (~30 min from nearest-matches)
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

        # 2.1. ESportsBattle deep schedule (full-day Volta via tournament endpoints)
        try:
            deep_matches = await self.esb.get_volta_deep_schedule()
            for m in deep_matches:
                key = _match_key(m)
                volta_confirmed.add(key)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
        except Exception as e:
            logger.warning("ESportsBattle deep schedule failed: %s", e)

        # 2.5. HUDstats: GG League schedule (30+ min lookahead, way ahead of BetsAPI)
        try:
            hudstats_matches = await self.hudstats.get_schedule()
            for m in hudstats_matches:
                key = _match_key(m)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
        except Exception as e:
            logger.warning("HUDstats schedule failed: %s", e)

        # Note: HUDstats result harvesting was already run above (before
        # resolve_pending_picks) to ensure finished matches are captured
        # as MatchResults before the pick resolver processes them.

        # 2.6. bwin: Volta schedule + odds (has pre-match odds well before kickoff)
        # bwin returns matches WITH odds pre-attached. If AceOdds/ESportsBattle
        # already added the same match (without odds), merge the odds in.
        try:
            bwin_matches = await self.bwin.get_volta_schedule()
            for m in bwin_matches:
                key = _match_key(m)
                volta_confirmed.add(key)
                if key not in seen_keys:
                    seen_keys.add(key)
                    key_to_idx[key] = len(all_upcoming)
                    all_upcoming.append(m)
                elif key in key_to_idx and m.odds and m.odds.has_data:
                    # Match already exists from AceOdds/ESB — transfer bwin odds
                    existing = all_upcoming[key_to_idx[key]]
                    if not (existing.odds and existing.odds.has_data):
                        existing.odds = m.odds
                        logger.info(
                            "Merged bwin odds into %s", existing.display_name,
                        )
        except Exception as e:
            logger.warning("bwin Volta schedule failed: %s", e)

        # 3. Kambi: schedule + odds in one shot (GG League, GT Leagues)
        # If HUDstats already added a GG League match (without odds),
        # merge the Kambi odds in — same pattern as bwin for Volta.
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
                elif key in key_to_idx and m.odds and m.odds.has_data:
                    # Match already exists from HUDstats — transfer Kambi odds
                    existing = all_upcoming[key_to_idx[key]]
                    if not (existing.odds and existing.odds.has_data):
                        existing.odds = m.odds
                        logger.info(
                            "Merged Kambi odds into %s", existing.display_name,
                        )
        except Exception as e:
            logger.warning("Kambi schedule failed: %s", e)

        # 4. Supplementary: BetsAPI for all leagues.  Even when Kambi
        # already provided schedule + odds, we still need BetsAPI to
        # cross-reference match IDs — picks with kambi_/hudstats_ IDs
        # can't resolve results via direct BetsAPI event view.
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
                        if existing.match_id.startswith(("esb_", "ace_", "hudstats_")):
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
            if m.match_id.startswith(("esb_", "ace_", "hudstats_", "kambi_"))
        ]
        if still_unresolved:
            betsapi_lookup: dict[tuple[str, str], list[tuple[int, UpcomingMatch]]] = {}
            for i, m in enumerate(all_upcoming):
                if m.match_id.startswith(("esb_", "ace_", "hudstats_", "kambi_")):
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

        # Enrich bare-handle names with team names from DB history.
        # When AceOdds/ESportsBattle fail, BetsAPI matches only have handles
        # like "Senya" — look up their last known team from the DB.
        self._enrich_team_names(all_upcoming)

        # Persist upcoming matches so the /api/schedule endpoint can
        # show them alongside completed results.
        self.db.save_upcoming(all_upcoming)

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
            if m.match_id.startswith(("esb_", "ace_", "hudstats_", "kambi_"))
        ]
        if still_unresolved:
            console.print(
                f"  [dim]{len(still_unresolved)} match(es) without BetsAPI ID "
                f"— trying last-minute lookup...[/dim]"
            )
            try:
                fresh: list[UpcomingMatch] = []
                # Query BetsAPI for all leagues to cross-reference IDs
                for lid in settings.tracked_league_ids:
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

        # ── Odds cascade ──────────────────────────────────────────
        # Priority order:
        # 1. BetsAPI (bet365) — broadest coverage, best for GG/GT leagues
        # 2. Kambi — fills gaps, especially for GG/GT
        # 3. bwin — primary Volta odds (available pre-match, unlike BetsAPI)
        # 4. FanDuel — supplementary for anything still missing

        # Skip kambi_/bwin_ matches: they already have odds from get_schedule()
        betsapi_matches = [
            m for m in all_upcoming
            if not m.match_id.startswith(("esb_", "ace_", "hudstats_", "kambi_", "bwin_"))
        ]
        if betsapi_matches:
            await self.api.fetch_odds_batch(betsapi_matches)

        # Kambi fills gaps for GG/GT leagues
        kambi_fallback = 0
        try:
            kambi_fallback = await self.kambi.attach_odds(all_upcoming)
        except Exception as e:
            logger.warning("kambi odds fetch failed: %s", e)

        # bwin provides odds for its own Volta tournaments (different player
        # pool from bet365).  attach_odds handles any cross-matches.
        bwin_attached = 0
        try:
            bwin_attached = await self.bwin.attach_odds(all_upcoming)
        except Exception as e:
            logger.warning("bwin odds fetch failed: %s", e)

        # FanDuel — last resort for anything still missing
        fanduel_attached = 0
        try:
            fanduel_attached = await self.fanduel.attach_odds(all_upcoming)
        except Exception as e:
            logger.warning("fanduel odds fetch failed: %s", e)

        # ── Volta BetsAPI polling ─────────────────────────────────
        # bet365's Volta odds (via BetsAPI) only appear ~2 min before
        # kickoff.  bwin has DIFFERENT Volta players, so it can't fill
        # this gap.  For Volta matches starting within ~8 min that still
        # lack odds, aggressively poll BetsAPI to catch them ASAP.
        _VOLTA_LID = 38439
        _VOLTA_RETRY_INTERVAL = 45   # seconds between retries
        _VOLTA_MAX_RETRIES = 4       # max polls (~3 min total)
        _VOLTA_WINDOW = 480          # only retry matches starting within 8 min

        now = time.time()
        volta_no_odds = [
            m for m in all_upcoming
            if m.league_id == _VOLTA_LID
            and not (m.odds and m.odds.has_data)
            and 0 < (m.start_time - now) <= _VOLTA_WINDOW
        ]
        if volta_no_odds and self._running:
            console.print(
                f"  [dim]{len(volta_no_odds)} Volta match(es) within "
                f"{_VOLTA_WINDOW // 60}min missing odds — polling BetsAPI "
                f"(up to {_VOLTA_MAX_RETRIES}x every {_VOLTA_RETRY_INTERVAL}s)...[/dim]"
            )

            for attempt in range(1, _VOLTA_MAX_RETRIES + 1):
                if not self._running:
                    break

                await asyncio.sleep(_VOLTA_RETRY_INTERVAL)

                # Fetch fresh in-play + upcoming from BetsAPI
                try:
                    fresh: list[UpcomingMatch] = []
                    fresh.extend(await self.api.get_inplay_matches(_VOLTA_LID))
                    fresh.extend(await self.api.get_upcoming_matches(_VOLTA_LID))
                except Exception as e:
                    logger.warning("Volta poll %d/%d failed: %s", attempt, _VOLTA_MAX_RETRIES, e)
                    continue

                # Build lookup by player pair for cross-referencing
                fresh_lookup: dict[tuple[str, str], list[UpcomingMatch]] = {}
                for fm in fresh:
                    h = extract_handle(fm.home).lower()
                    a = extract_handle(fm.away).lower()
                    pair = tuple(sorted([h, a]))
                    fresh_lookup.setdefault(pair, []).append(fm)

                # Resolve ace_/esb_ IDs to BetsAPI numeric IDs
                still_missing = [
                    m for m in volta_no_odds
                    if not (m.odds and m.odds.has_data)
                ]
                if not still_missing:
                    break

                resolved = 0
                for m in still_missing:
                    if m.match_id.startswith(("esb_", "ace_", "hudstats_")):
                        h = extract_handle(m.home).lower()
                        a = extract_handle(m.away).lower()
                        pair = tuple(sorted([h, a]))
                        for candidate in fresh_lookup.get(pair, []):
                            if abs(candidate.start_time - m.start_time) <= 300:
                                m.match_id = candidate.match_id
                                resolved += 1
                                break

                # Fetch odds for matches with numeric BetsAPI IDs
                retry_targets = [
                    m for m in still_missing
                    if not m.match_id.startswith(("esb_", "ace_", "hudstats_", "kambi_", "bwin_"))
                    and not (m.odds and m.odds.has_data)
                ]
                if retry_targets:
                    await self.api.fetch_odds_batch(
                        retry_targets, skip_bet365_prematch=True,
                    )

                got = sum(1 for m in volta_no_odds if m.odds and m.odds.has_data)
                remaining = len(volta_no_odds) - got
                if got:
                    console.print(
                        f"  [dim]Volta poll {attempt}/{_VOLTA_MAX_RETRIES}: "
                        f"got odds for {got}/{len(volta_no_odds)} "
                        f"(resolved {resolved} ID(s))[/dim]"
                    )
                if remaining == 0:
                    break

        # Report odds coverage
        odds_count = sum(1 for m in all_upcoming if m.odds and m.odds.has_data)
        if odds_count:
            sources = []
            kambi_total = sum(
                1 for m in all_upcoming
                if m.odds and m.odds.has_data and m.match_id.startswith("kambi_")
            ) + kambi_fallback
            bwin_total = sum(
                1 for m in all_upcoming
                if m.odds and m.odds.has_data and m.match_id.startswith("bwin_")
            ) + bwin_attached
            betsapi_odds = odds_count - kambi_total - bwin_total - fanduel_attached
            if betsapi_odds > 0:
                sources.append(f"BetsAPI: {betsapi_odds}")
            if kambi_total > 0:
                sources.append(f"Kambi: {kambi_total}")
            if bwin_total > 0:
                sources.append(f"bwin: {bwin_total}")
            if fanduel_attached > 0:
                sources.append(f"FanDuel: {fanduel_attached}")
            console.print(
                f"  [dim]Got odds for {odds_count}/{len(all_upcoming)} "
                f"matches ({', '.join(sources)})[/dim]"
            )
        else:
            no_id = sum(
                1 for m in all_upcoming
                if m.match_id.startswith(("esb_", "ace_", "hudstats_"))
            )
            console.print(
                f"  [yellow]No odds for any of {len(all_upcoming)} match(es). "
                f"{no_id} still without BetsAPI ID, "
                f"kambi={kambi_fallback} bwin={bwin_attached} fanduel={fanduel_attached}[/yellow]"
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

        # Log diagnostic for non-Volta leagues — surface why picks are/aren't generated
        _VOLTA = 38439
        for r in reports:
            if r.match.league_id == _VOLTA:
                continue
            odds = r.match.odds
            has_odds = odds is not None and odds.has_data
            ou_lines = sorted(ol.line for ol in odds.total_lines) if has_odds and odds.total_lines else []
            has_ml = bool(odds and odds.moneyline) if has_odds else False
            spread_vals = sorted(s.handicap for s in odds.spreads) if has_odds and odds.spreads else []
            league_tag = "GT" if r.match.league_id in NO_TOTALS_LEAGUES else "GG"
            logger.info(
                "%s diagnostic: %s vs %s — odds=%s O/U=%s ml=%s spreads=%s "
                "trends=%d best_bet=%s",
                league_tag,
                extract_handle(r.match.home),
                extract_handle(r.match.away),
                has_odds, ou_lines, has_ml, spread_vals,
                len(r.trends),
                r.best_bet.market if r.best_bet else "None",
            )
            if r.trends and not r.best_bet:
                for t in r.trends:
                    logger.info(
                        "  %s trend: %s — %.0f%% (%d/%d)",
                        league_tag, t.category, t.hit_rate * 100,
                        t.hits, t.sample_size,
                    )

        # Display results — only picks backed by real sportsbook odds
        reports_with_picks = [r for r in reports if r.best_bet is not None]

        # Per-league DB counts help diagnose empty-history issues
        league_db_counts = {
            lid: self.db.total_matches_for_league(lid)
            for lid in settings.tracked_league_ids
        }

        display_scan_summary(
            total_matches=len(all_upcoming),
            matches_with_picks=len(reports_with_picks),
            total_trends=sum(len(r.trends) for r in reports_with_picks),
            db_total=self.db.total_matches(),
            league_db_counts=league_db_counts,
        )

        display_reports_by_league(reports)

        # Record new picks. Uses content-based _match_key (players +
        # start_time) instead of match_id, which can change between scans
        # when BetsAPI cross-references an ace_/esb_ match with a different
        # numeric ID.  Discord alert delivery is handled by the Discord bot
        # layer (discord_bot.py); this loop only persists picks to the DB.
        alerted_count = 0
        new_reports = [
            r for r in reports_with_picks
            if _match_key(r.match) not in self._alerted_keys
        ]
        if new_reports:
            new_reports.sort(key=lambda r: r.match.start_time)
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

        # Auto-update CSV data export after every scan
        from esporf.export import export_all
        export_all(self.db)

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
            f"  Schedule: [bold green]AceOdds[/bold green] + ESportsBattle + [bold green]HUDstats[/bold green] + [bold cyan]Kambi[/bold cyan] + BetsAPI\n"
            f"  External: [bold cyan]TotalCorner[/bold cyan] + Forebet\n"
            f"  Odds: BetsAPI (bet365) + [bold cyan]Kambi[/bold cyan] + [bold green]bwin[/bold green] (Volta) + [bold cyan]FanDuel[/bold cyan]\n"
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
            await self.hudstats.close()
            await self.bwin.close()
            await self.fanduel.close()
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
        await bot.hudstats.close()
        await bot.bwin.close()
        await bot.fanduel.close()
        await bot.tc.close()
        await bot.forebet.close()
        bot.db.close()
