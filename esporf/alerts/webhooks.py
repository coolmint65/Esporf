"""Webhook-based alert delivery for trend signals (Discord)."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

import httpx

from esporf.config import settings
from esporf.models import MatchupReport, extract_handle, extract_team, league_display_name

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
    """Build a clean Discord embed card for an odds-backed bet pick."""
    match = report.match

    pick = report.best_bet
    if not pick:
        return {}

    # Hard guard — never build an embed for a non-tracked league
    if match.league_id not in set(settings.tracked_league_ids):
        logger.warning(
            "Blocked embed for non-tracked league %d (%s)",
            match.league_id, match.display_name,
        )
        return {}

    league_name = league_display_name(match.league_id)
    ts = match.start_time

    top_rate = max(t.hit_rate for t in pick.supporting_trends)
    total_hits = sum(t.hits for t in pick.supporting_trends)
    total_sample = sum(t.sample_size for t in pick.supporting_trends)

    home_handle = extract_handle(match.home)
    away_handle = extract_handle(match.away)
    home_team = extract_team(match.home)
    away_team = extract_team(match.away)

    home_display = f"{home_handle} ({home_team})" if home_team else home_handle
    away_display = f"{away_handle} ({away_team})" if away_team else away_handle

    # Show form quality indicator when it deviates from baseline
    form_str = ""
    if report.form_modifier >= 1.10:
        form_str = f"  |  Form: {report.form_modifier:.2f}x"
    elif report.form_modifier <= 0.90:
        form_str = f"  |  Form: {report.form_modifier:.2f}x"

    lines = [
        f"### {home_display}  vs  {away_display}",
        f"### Kickoff: <t:{ts}:t>  (<t:{ts}:R>)",
        "",
        f"## {pick.market.upper()}  —  {pick.units_display}",
        "",
        f"**{top_rate:.0%}** hit rate  ({total_hits}/{total_sample}){form_str}",
    ]

    color = _confidence_color(top_rate)

    return {
        "title": league_name,
        "description": "\n".join(lines),
        "color": color,
    }


# ── Senders ──────────────────────────────────────────────────────


async def send_discord_alert(reports: list[MatchupReport]) -> None:
    """Send odds-backed bet alerts to a Discord channel via webhook embeds."""
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
                logger.info("Discord alert sent for %s", report.match.display_name)
        except Exception as e:
            logger.warning("Failed to send Discord alert: %s", e)


async def send_alerts(reports: list[MatchupReport]) -> None:
    """Send alerts through Discord for odds-backed picks only.

    Only sends alerts for matches that have real sportsbook odds
    and belong to a tracked league.
    """
    tracked = set(settings.tracked_league_ids)
    alertable = [
        r for r in reports
        if r.best_bet is not None
        and r.match.league_id in tracked
    ]
    if not alertable:
        return

    if settings.discord_webhook_url:
        await send_discord_alert(alertable)
