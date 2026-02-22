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

    # ── Upcoming matches ─────────────────────────────────────────────

    async def get_upcoming_matches(self, league_id: int) -> list[UpcomingMatch]:
        """Fetch upcoming (pre-match) events for a league."""
        data = await self._request(
            "/events/upcoming",
            params={"sport_id": SPORT_ID, "league_id": league_id},
        )
        return [self._parse_upcoming(ev, league_id) for ev in data.get("results", [])]

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
        seen_lines: set[float] = set()
        for entry in odds_data.get("1_3", []):
            for line in self._parse_ou_odds(entry):
                if line.line not in seen_lines:
                    seen_lines.add(line.line)
                    odds.total_lines.append(line)

        # Parse 1X2 moneyline (market 1_1)
        ml_entries = odds_data.get("1_1", [])
        if ml_entries:
            ml = self._parse_moneyline(ml_entries[-1])  # latest entry
            if ml:
                odds.moneyline = ml

        return odds

    async def fetch_odds_batch(
        self, matches: list[UpcomingMatch], delay: float = 0.3
    ) -> None:
        """Fetch odds for a batch of matches, attaching results to each.

        Adds a small delay between API calls to respect rate limits.
        Modifies matches in-place by setting their ``odds`` attribute.
        """
        for match in matches:
            try:
                new_odds = await self.get_event_odds(match.match_id)
                if new_odds.has_data:
                    match.odds = new_odds
                    lines = match.odds.available_lines
                    logger.info(
                        "Odds for %s: lines=%s", match.display_name, lines
                    )
                else:
                    logger.debug("No odds data for %s", match.display_name)
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
        # Use the event's actual league ID when available — BetsAPI may
        # return matches from neighbouring leagues (e.g. GT Leagues under
        # a Volta query).
        actual_league_id = league_id
        event_league = event.get("league", {})
        if event_league:
            try:
                actual_league_id = int(event_league.get("id", league_id))
            except (ValueError, TypeError):
                pass
        return UpcomingMatch(
            match_id=str(event.get("id", "")),
            league_id=actual_league_id,
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
