"""Webhook-based alert delivery for trend signals (Discord)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from esporf.config import settings
from esporf.models import MatchupReport, extract_handle

logger = logging.getLogger(__name__)

# ── Formatting helpers ───────────────────────────────────────────

_EST = ZoneInfo("US/Eastern")

# Hit-rate-to-color mapping (Discord embed hex colors)
_COLOR_TIERS = [
    (0.80, 0x2ECC71),  # green — 80%+
    (0.70, 0xFEE75C),  # yellow — 70-79%
    (0.65, 0xE67E22),  # orange — 65-70%
    (0.00, 0xED4245),  # red — below 65% (safety)
]


def _confidence_color(confidence: float) -> int:
    for threshold, color in _COLOR_TIERS:
        if confidence >= threshold:
            return color
    return 0x95A5A6


def _build_discord_embed(report: MatchupReport) -> dict:
    """Build a clean Discord embed card for a bet pick.

    Works with both real-odds picks (best_bet) and trend-only picks
    (best_trend_pick). Trend-only embeds include implied fair odds
    so the user can compare against their sportsbook.
    """
    match = report.match

    # Use real pick if available, otherwise trend-only pick
    pick = report.best_bet or report.best_trend_pick
    if not pick:
        return {}

    is_trend_only = report.best_bet is None

    # Hard guard — never build an embed for a non-tracked league
    if match.league_id not in set(settings.tracked_league_ids):
        logger.warning(
            "Blocked embed for non-tracked league %d (%s)",
            match.league_id, match.display_name,
        )
        return {}

    league = match.league
    league_name = league.display_name if league else f"League {match.league_id}"
    ts = match.start_time

    top_rate = max(t.hit_rate for t in pick.supporting_trends)
    total_hits = sum(t.hits for t in pick.supporting_trends)
    total_sample = sum(t.sample_size for t in pick.supporting_trends)

    home_display = extract_handle(match.home)
    away_display = extract_handle(match.away)

    if is_trend_only:
        # Trend-only alert: show fair price so user can compare
        from esporf.models import _decimal_to_american
        fair_american = _decimal_to_american(1.0 / top_rate) if top_rate > 0 else "N/A"

        lines = [
            f"### {home_display}  vs  {away_display}",
            f"### Kickoff: <t:{ts}:t>  (<t:{ts}:R>)",
            "",
            f"## {pick.market.upper()}",
            "",
            f"**{top_rate:.0%}** hit rate  ({total_hits}/{total_sample})",
            f"Fair price: **{fair_american}**  (anything better is +EV)",
            "",
            "Check your sportsbook for live odds",
        ]
    else:
        lines = [
            f"### {home_display}  vs  {away_display}",
            f"### Kickoff: <t:{ts}:t>  (<t:{ts}:R>)",
            "",
            f"## {pick.market.upper()}  —  {pick.units_display}",
            "",
            f"**{top_rate:.0%}** hit rate  ({total_hits}/{total_sample})",
        ]

    color = _confidence_color(top_rate)

    return {
        "title": league_name,
        "description": "\n".join(lines),
        "color": color,
    }


# ── Senders ──────────────────────────────────────────────────────


async def send_discord_alert(reports: list[MatchupReport]) -> None:
    """Send trend alerts to a Discord channel via webhook embeds.

    Sends alerts for both real-odds picks and trend-only picks.
    Trend-only picks get sent when there are strong trends but no
    sportsbook odds yet, giving the user early notice to check their book.
    """
    url = settings.discord_webhook_url
    if not url:
        return

    for report in reports:
        if not report.has_trends:
            continue
        embed = _build_discord_embed(report)
        if not embed:
            continue
        payload: dict = {"embeds": [embed]}
        if settings.discord_role_id:
            payload["content"] = f"<@&{settings.discord_role_id}>"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                pick_type = "trend-only" if report.best_bet is None else "odds-backed"
                logger.info("Discord alert (%s) sent for %s", pick_type, report.match.display_name)
        except Exception as e:
            logger.warning("Failed to send Discord alert: %s", e)


async def send_alerts(reports: list[MatchupReport]) -> None:
    """Send alerts through Discord.

    Sends alerts for matches with either:
    - Real odds-backed picks (best_bet), or
    - Trend-only picks (best_trend_pick) when odds aren't available yet

    Only sends alerts for matches belonging to a tracked league —
    this is the final gate that prevents GT Leagues / GG League
    alerts from reaching Discord even if upstream filters miss them.
    """
    tracked = set(settings.tracked_league_ids)
    alertable = [
        r for r in reports
        if r.has_trends
        and r.match.league_id in tracked
        and (r.best_bet is not None or r.best_trend_pick is not None)
    ]
    if not alertable:
        return

    if settings.discord_webhook_url:
        await send_discord_alert(alertable)
