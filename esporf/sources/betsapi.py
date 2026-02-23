"""BetsAPI client for fetching eSoccer match results, upcoming fixtures, and odds.

Primary data source for building match history. Fetches:
- Ended matches with scores (for historical trend database)
- Upcoming matches (to know who's playing next and run trend analysis)
- In-play matches (for live alerting)
- Event odds (Over/Under lines, moneylines from sportsbooks)
- Day-based schedule (for longer lookahead)

Docs: https://betsapi.com/docs/
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from esporf.config import settings
from esporf.models import MatchOdds, MatchResult, MoneylineOdds, OddsLine, UpcomingMatch

logger = logging.getLogger(__name__)

SPORT_ID = 1  # Soccer (eSoccer is categorized under soccer)

# BetsAPI base URL without version prefix (for v1/v2 endpoints)
_API_ROOT = "https://api.b365api.com"


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

    async def _request_raw(self, url: str, params: dict[str, Any] | None = None) -> dict:
        """Make a request to a full URL (for v1/v2 endpoints outside the v3 base)."""
        if not self.token:
            raise ValueError("BetsAPI token not configured.")
        all_params = {"token": self.token}
        if params:
            all_params.update(params)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=all_params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("success") == 0:
            error = data.get("error", "Unknown BetsAPI error")
            raise RuntimeError(f"BetsAPI error: {error}")

        return data

    # ── Ended matches (for building history) ─────────────────────────

    async def get_ended_matches(self, league_id: int, page: int = 1) -> list[MatchResult]:
        """Fetch completed matches with final scores for a league.

        Each page returns ~50 results. Use multiple pages to backfill history.
        """
        data = await self._request(
            "/events/ended",
            params={"sport_id": SPORT_ID, "league_id": league_id, "page": page},
        )
        results = []
        for ev in data.get("results", []):
            if not self._event_matches_league(ev, league_id):
                continue
            match = self._parse_ended_match(ev, league_id)
            if match is not None:
                results.append(match)
        return results

    async def backfill_history(self, league_id: int, pages: int = 10) -> list[MatchResult]:
        """Fetch multiple pages of ended matches to build up historical database."""
        all_results: list[MatchResult] = []
        for page in range(1, pages + 1):
            try:
                matches = await self.get_ended_matches(league_id, page=page)
                if not matches:
                    break  # no more results
                all_results.extend(matches)
                logger.info(
                    "Fetched page %d for league %d: %d matches", page, league_id, len(matches)
                )
            except Exception as e:
                logger.warning("Failed to fetch page %d for league %d: %s", page, league_id, e)
                break
        return all_results

    # ── League verification ────────────────────────────────────────

    @staticmethod
    def _event_matches_league(event: dict, league_id: int) -> bool:
        """Check if a BetsAPI event actually belongs to the requested league.

        BetsAPI sometimes returns events from neighbouring leagues
        (e.g. GT Leagues or GG League under a Volta query).  We check
        both the numeric league ID *and* the league name when available.
        """
        event_league = event.get("league", {})
        if not isinstance(event_league, dict) or not event_league:
            return True  # no league info available — assume it matches

        # 1. Numeric ID check
        lid = event_league.get("id")
        if lid is not None:
            try:
                if int(lid) != league_id:
                    return False
            except (ValueError, TypeError):
                pass

        # 2. Name-keyword check — catches misclassified events where the
        #    numeric ID matches but the name reveals the real league.
        name = str(event_league.get("name", "")).lower()
        if name:
            _REQUIRED_KEYWORDS: dict[int, str] = {
                38439: "volta",   # eSoccer Battle - Volta - 6 Mins Play
                23114: "gt",      # eSoccer GT Leagues - 12 Mins Play
                37298: "gg",      # eSoccer GG League - 8 Mins Play
            }
            required = _REQUIRED_KEYWORDS.get(league_id)
            if required and required not in name:
                logger.debug(
                    "Skipping event %s — league name '%s' missing keyword '%s'",
                    event.get("id"), name, required,
                )
                return False

        return True

    # ── Upcoming matches ─────────────────────────────────────────────

    async def get_upcoming_matches(self, league_id: int) -> list[UpcomingMatch]:
        """Fetch upcoming (pre-match) events for a league."""
        data = await self._request(
            "/events/upcoming",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        return [
            self._parse_upcoming(ev, league_id)
            for ev in data.get("results", [])
            if self._event_matches_league(ev, league_id)
        ]

    async def get_inplay_matches(self, league_id: int) -> list[UpcomingMatch]:
        """Fetch live / in-play events for a league.

        BetsAPI sometimes lists eSoccer matches as in-play before they've
        actually kicked off (the match appears only ~2 min before start and
        is immediately flagged live).  We use ``start_time`` to determine
        the real status: if kickoff is still in the future, treat the match
        as upcoming rather than live.
        """
        data = await self._request(
            "/events/inplay",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        now = int(time.time())
        matches = []
        for ev in data.get("results", []):
            if not self._event_matches_league(ev, league_id):
                continue
            m = self._parse_upcoming(ev, league_id)
            # Only trust the "live" designation when kickoff is in the past
            m.is_live = m.start_time <= now
            matches.append(m)
        return matches

    # ── Extended schedule (day-based lookahead) ────────────────────

    async def get_upcoming_by_day(
        self, league_id: int, day: str | None = None
    ) -> list[UpcomingMatch]:
        """Fetch upcoming matches for a specific day (YYYYMMDD format).

        The ``day`` parameter tells BetsAPI to return ALL scheduled events
        for that calendar day, potentially hours in advance.  Without it,
        the default endpoint only shows events a few minutes before kickoff.

        If *day* is None, queries today and tomorrow (UTC) to cover timezone
        boundaries.
        """
        all_matches: list[UpcomingMatch] = []
        seen: set[str] = set()

        if day:
            days = [day]
        else:
            now_utc = datetime.now(tz=timezone.utc)
            days = [
                now_utc.strftime("%Y%m%d"),
                (now_utc + timedelta(days=1)).strftime("%Y%m%d"),
            ]

        for d in days:
            try:
                data = await self._request(
                    "/events/upcoming",
                    params={
                        "sport_id": SPORT_ID,
                        "league_id": league_id,
                        "day": d,
                    },
                )
                for ev in data.get("results", []):
                    if not self._event_matches_league(ev, league_id):
                        continue
                    m = self._parse_upcoming(ev, league_id)
                    if m.match_id not in seen:
                        seen.add(m.match_id)
                        all_matches.append(m)
            except Exception as e:
                logger.warning("Day schedule fetch failed for %s: %s", d, e)

        return all_matches

    async def get_full_schedule(self, league_id: int) -> list[UpcomingMatch]:
        """Get the broadest possible schedule by combining all BetsAPI sources.

        Merges: day-based upcoming (today + tomorrow) + standard upcoming
        + in-play. Deduplicates by match ID.

        Note: For Volta matches, the bot also uses ESportsBattle as a
        supplementary schedule source (see bot.py), since BetsAPI only
        lists Volta matches ~2 min before kickoff.
        """
        seen: set[str] = set()
        all_matches: list[UpcomingMatch] = []

        # 1. Day-based (broadest lookahead)
        try:
            day_matches = await self.get_upcoming_by_day(league_id)
            for m in day_matches:
                if m.match_id not in seen:
                    seen.add(m.match_id)
                    all_matches.append(m)
        except Exception as e:
            logger.warning("Day schedule failed for league %d: %s", league_id, e)

        # 2. Standard upcoming (catches anything day-based missed)
        try:
            upcoming = await self.get_upcoming_matches(league_id)
            for m in upcoming:
                if m.match_id not in seen:
                    seen.add(m.match_id)
                    all_matches.append(m)
        except Exception as e:
            logger.warning("Upcoming fetch failed for league %d: %s", league_id, e)

        # 3. In-play (catches matches that jumped straight to live)
        now = int(time.time())
        try:
            inplay = await self.get_inplay_matches(league_id)
            for m in inplay:
                if m.match_id not in seen:
                    seen.add(m.match_id)
                    m.is_live = m.start_time <= now
                    all_matches.append(m)
        except Exception as e:
            logger.warning("Inplay fetch failed for league %d: %s", league_id, e)

        # Drop events that actually belong to a different league
        all_matches = [m for m in all_matches if m.league_id == league_id]

        # Sort by start time
        all_matches.sort(key=lambda m: m.start_time)
        return all_matches

    # ── Odds ─────────────────────────────────────────────────────────

    async def get_event_odds(self, event_id: str) -> MatchOdds:
        """Fetch pre-match odds for a specific event.

        Uses the /v2/event/odds endpoint with markets:
        - 1_3: Over/Under (Goal Line) — the actual lines offered
        - 1_1: 1X2 (Moneyline)

        Returns a MatchOdds with all available lines and moneyline odds.
        """
        odds = MatchOdds()

        # Fetch all available odds (no market filter — comma-separated
        # odds_market doesn't work reliably, and we parse selectively anyway)
        try:
            data = await self._request_raw(
                f"{_API_ROOT}/v2/event/odds",
                params={"event_id": event_id},
            )
        except Exception as e:
            logger.debug("Odds fetch failed for event %s: %s", event_id, e)
            return odds

        results = data.get("results", {})
        odds_data = results.get("odds", results)

        # Parse Over/Under lines (market 1_3)
        # BetsAPI v2 can return odds either as a flat list or as a dict
        # grouped by source (e.g. {"17_1": [...], "17_2": [...]}).
        # Normalize both formats into a flat list of odds entries.
        raw_ou = odds_data.get("1_3", [])
        ou_entries: list[dict] = []
        if isinstance(raw_ou, dict):
            # Grouped by source — flatten all sources, preferring bet365
            for source_entries in raw_ou.values():
                if isinstance(source_entries, list):
                    ou_entries.extend(source_entries)
        elif isinstance(raw_ou, list):
            ou_entries = raw_ou

        seen_lines: set[float] = set()
        for entry in ou_entries:
            if not isinstance(entry, dict):
                continue
            for line in self._parse_ou_odds(entry):
                if line.line not in seen_lines:
                    seen_lines.add(line.line)
                    odds.total_lines.append(line)

        # Parse 1X2 moneyline (market 1_1)
        raw_ml = odds_data.get("1_1", [])
        ml_entries: list[dict] = []
        if isinstance(raw_ml, dict):
            for source_entries in raw_ml.values():
                if isinstance(source_entries, list):
                    ml_entries.extend(source_entries)
        elif isinstance(raw_ml, list):
            ml_entries = raw_ml

        if ml_entries:
            last = ml_entries[-1]
            if isinstance(last, dict):
                ml = self._parse_moneyline(last)
                if ml:
                    odds.moneyline = ml

        if not odds.has_data:
            logger.debug(
                "Odds response for event %s had no parseable data (keys: %s)",
                event_id, list(odds_data.keys()) if isinstance(odds_data, dict) else "N/A",
            )

        return odds

    async def get_event_odds_summary(self, event_id: str) -> MatchOdds:
        """Fetch latest odds snapshot via /v2/event/odds/summary.

        This endpoint returns only the most recent odds for an event,
        which may be populated before the full odds history endpoint.
        Same response format as /v2/event/odds but with only the latest entry.
        """
        odds = MatchOdds()
        try:
            data = await self._request_raw(
                f"{_API_ROOT}/v2/event/odds/summary",
                params={"event_id": event_id},
            )
        except Exception as e:
            logger.debug("Odds summary failed for event %s: %s", event_id, e)
            return odds

        results = data.get("results", {})
        odds_data = results.get("odds", results)

        seen_lines: set[float] = set()
        for entry in odds_data.get("1_3", []):
            for line in self._parse_ou_odds(entry):
                if line.line not in seen_lines:
                    seen_lines.add(line.line)
                    odds.total_lines.append(line)

        ml_entries = odds_data.get("1_1", [])
        if ml_entries:
            ml = self._parse_moneyline(ml_entries[-1])
            if ml:
                odds.moneyline = ml

        return odds

    async def get_bet365_prematch_odds(self, event_id: str) -> MatchOdds:
        """Fetch prematch odds from bet365's direct data feed.

        Uses /v3/bet365/prematch which reads directly from bet365's
        prematch market. This may have odds available before the generic
        /v2/event/odds endpoint since it taps bet365's prematch pipeline.

        The FI parameter accepts the BetsAPI event_id — b365api maps it
        internally to bet365's fixture ID.
        """
        odds = MatchOdds()
        try:
            data = await self._request(
                "/bet365/prematch",
                params={"FI": event_id},
            )
        except Exception as e:
            logger.debug("bet365 prematch failed for %s: %s", event_id, e)
            return odds

        results = data.get("results", {})
        if not results:
            return odds

        # Log raw structure on first success so we can refine parsing
        logger.debug(
            "bet365 prematch raw keys for %s: %s",
            event_id,
            list(results.keys()) if isinstance(results, dict) else type(results).__name__,
        )

        return self._parse_bet365_prematch(results)

    @staticmethod
    def _parse_bet365_prematch(results: dict | list) -> MatchOdds:
        """Parse bet365 prematch response into MatchOdds.

        The bet365 prematch format is a hierarchical tree:
        - Market groups contain markets
        - Markets contain selections with OD (odds), HA (handicap), NA (name)

        We look for Over/Under (Goals) and Full Time Result (1X2).
        """
        odds = MatchOdds()

        # results can be a dict with market data or a list of market groups
        items = results if isinstance(results, list) else [results]

        # Flatten: collect all nested dicts that look like market data
        all_nodes: list[dict] = []
        _collect_bet365_nodes(items, all_nodes)

        seen_lines: set[float] = set()
        home_ml = away_ml = draw_ml = 0.0

        for node in all_nodes:
            na = str(node.get("NA", "")).strip()
            od_raw = node.get("OD", "")
            ha = node.get("HA", "")

            # Parse fractional or decimal odds
            dec_odds = _parse_bet365_odds(od_raw)
            if dec_odds is None or dec_odds <= 1.0:
                continue

            na_lower = na.lower()

            # Over/Under goals
            if na_lower.startswith("over") and ha:
                try:
                    line = float(ha)
                    if line > 0 and round(line % 1, 2) == 0.5 and line not in seen_lines:
                        # We'll pair with Under later
                        seen_lines.add(line)
                        odds.total_lines.append(
                            OddsLine(line=line, over_odds=dec_odds, under_odds=1.95, source="bet365")
                        )
                except (ValueError, TypeError):
                    pass
            elif na_lower.startswith("under") and ha:
                try:
                    line = float(ha)
                    if line > 0 and round(line % 1, 2) == 0.5:
                        # Update existing line with real under odds
                        for ol in odds.total_lines:
                            if ol.line == line:
                                ol.under_odds = dec_odds
                                break
                        else:
                            # Under came before Over — add placeholder
                            if line not in seen_lines:
                                seen_lines.add(line)
                                odds.total_lines.append(
                                    OddsLine(line=line, over_odds=1.85, under_odds=dec_odds, source="bet365")
                                )
                except (ValueError, TypeError):
                    pass

            # 1X2 moneyline
            elif na_lower in ("1", "home", "draw", "x", "2", "away"):
                if na_lower in ("1", "home"):
                    home_ml = dec_odds
                elif na_lower in ("x", "draw"):
                    draw_ml = dec_odds
                elif na_lower in ("2", "away"):
                    away_ml = dec_odds

        if home_ml > 0 and away_ml > 0:
            odds.moneyline = MoneylineOdds(
                home_odds=home_ml,
                draw_odds=draw_ml,
                away_odds=away_ml,
                source="bet365",
            )

        if odds.has_data:
            logger.info(
                "bet365 prematch yielded %d O/U line(s), ML=%s",
                len(odds.total_lines),
                "yes" if odds.moneyline else "no",
            )

        return odds

    async def fetch_odds_batch(
        self, matches: list[UpcomingMatch], delay: float = 0.3
    ) -> None:
        """Fetch odds for a batch of matches, attaching results to each.

        Cascades through three endpoints for maximum coverage:
        1. /v2/event/odds — standard odds history
        2. /v2/event/odds/summary — latest snapshot (may arrive earlier)
        3. /v3/bet365/prematch — direct bet365 prematch feed

        Adds a small delay between API calls to respect rate limits.
        Modifies matches in-place by setting their ``odds`` attribute.
        """
        for match in matches:
            try:
                # 1. Standard odds endpoint
                new_odds = await self.get_event_odds(match.match_id)
                if new_odds.has_data:
                    match.odds = new_odds
                    logger.info(
                        "Odds for %s via v2/odds: lines=%s",
                        match.display_name, match.odds.available_lines,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue

                if delay > 0:
                    await asyncio.sleep(delay)

                # 2. Odds summary fallback
                new_odds = await self.get_event_odds_summary(match.match_id)
                if new_odds.has_data:
                    match.odds = new_odds
                    logger.info(
                        "Odds for %s via v2/odds/summary: lines=%s",
                        match.display_name, match.odds.available_lines,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue

                if delay > 0:
                    await asyncio.sleep(delay)

                # 3. bet365 prematch feed
                new_odds = await self.get_bet365_prematch_odds(match.match_id)
                if new_odds.has_data:
                    match.odds = new_odds
                    logger.info(
                        "Odds for %s via bet365/prematch: lines=%s",
                        match.display_name, match.odds.available_lines,
                    )
                else:
                    logger.debug("No odds from any source for %s", match.display_name)

            except Exception as e:
                logger.debug("Odds fetch error for %s: %s", match.display_name, e)
            if delay > 0:
                await asyncio.sleep(delay)

    # ── Parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_ended_match(event: dict, league_id: int) -> MatchResult | None:
        """Parse a BetsAPI ended event into a MatchResult with scores."""
        home_info = event.get("home", {})
        away_info = event.get("away", {})
        scores = event.get("scores", {})

        # Full-time score is typically under key "2"
        ft = scores.get("2", {})
        home_score = _safe_int(ft.get("home"))
        away_score = _safe_int(ft.get("away"))

        if home_score is None or away_score is None:
            return None  # skip matches without valid scores

        # Half-time score under key "1"
        ht = scores.get("1", {})

        return MatchResult(
            match_id=str(event.get("id", "")),
            league_id=league_id,
            home=home_info.get("name", "Unknown"),
            away=away_info.get("name", "Unknown"),
            home_score=home_score,
            away_score=away_score,
            start_time=int(event.get("time", 0)),
            ht_home_score=_safe_int(ht.get("home")),
            ht_away_score=_safe_int(ht.get("away")),
        )

    @staticmethod
    def _parse_upcoming(event: dict, league_id: int) -> UpcomingMatch:
        home_info = event.get("home", {})
        away_info = event.get("away", {})
        return UpcomingMatch(
            match_id=str(event.get("id", "")),
            league_id=league_id,
            home=home_info.get("name", "Unknown"),
            away=away_info.get("name", "Unknown"),
            start_time=int(event.get("time", 0)),
        )

    @staticmethod
    def _parse_ou_odds(entry: dict) -> list[OddsLine]:
        """Parse a single Over/Under odds entry from BetsAPI.

        BetsAPI uses ``over_od``/``under_od`` for O/U markets (not home/away).
        The handicap can be a compound Asian total like "3.5,4.0" — we split
        those into individual lines so each can be matched to trend data.

        All valid positive lines are accepted (.5, whole numbers, quarter
        lines).  The trend analysis handles any line value — it checks the
        historical hit rate at that specific threshold.
        """
        results: list[OddsLine] = []
        try:
            handicap_str = str(entry.get("handicap", "0"))
            over = float(entry.get("over_od", entry.get("home_od", 0)))
            under = float(entry.get("under_od", entry.get("away_od", 0)))
            if over <= 0 or under <= 0:
                return results

            # Handle compound handicap ("3.5,4.0") and simple ("4.5")
            for part in handicap_str.split(","):
                part = part.strip()
                if not part:
                    continue
                line = float(part)
                if line > 0:
                    results.append(OddsLine(line=line, over_odds=over, under_odds=under))
        except (ValueError, TypeError):
            pass
        return results

    @staticmethod
    def _parse_moneyline(entry: dict) -> MoneylineOdds | None:
        """Parse a 1X2 moneyline odds entry from BetsAPI."""
        try:
            home = float(entry.get("home_od", 0))
            draw = float(entry.get("draw_od", entry.get("neutral_od", 0)))
            away = float(entry.get("away_od", 0))
            if home > 0 and away > 0:
                return MoneylineOdds(
                    home_odds=home,
                    draw_odds=draw if draw > 0 else 0.0,
                    away_odds=away,
                )
        except (ValueError, TypeError):
            pass
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _collect_bet365_nodes(items: list | dict, out: list[dict]) -> None:
    """Recursively collect all dict nodes from a bet365 prematch response.

    The bet365 prematch format is a nested tree of market groups, markets,
    and selections. This flattens everything so we can scan for odds data.
    """
    if isinstance(items, dict):
        out.append(items)
        for v in items.values():
            if isinstance(v, (list, dict)):
                _collect_bet365_nodes(v, out)
    elif isinstance(items, list):
        for item in items:
            if isinstance(item, (list, dict)):
                _collect_bet365_nodes(item, out)


def _parse_bet365_odds(od_raw: Any) -> float | None:
    """Parse bet365 odds value which can be decimal or fractional (e.g. '2/1')."""
    if od_raw is None:
        return None
    s = str(od_raw).strip()
    if not s:
        return None

    # Fractional: "2/1" → 3.0
    if "/" in s:
        try:
            num, den = s.split("/", 1)
            return float(num) / float(den) + 1.0
        except (ValueError, ZeroDivisionError):
            return None

    # Decimal: "1.85"
    try:
        return float(s)
    except ValueError:
        return None
