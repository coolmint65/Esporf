"""Webhook-based alert delivery for trend signals (Discord, Telegram)."""

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


def _kickoff_est(ts: int) -> str:
    """Format a unix timestamp as '9:25 PM EST'."""
    dt = datetime.fromtimestamp(ts, tz=_EST)
    return dt.strftime("%-I:%M %p EST")


def _build_discord_embed(report: MatchupReport) -> dict:
    """Build a clean Discord embed card for a bet pick."""
    match = report.match
    pick = report.best_bet
    if not pick:
        return {}

    league = match.league
    league_name = league.display_name if league else f"League {match.league_id}"
    ts = match.start_time

    # History stats
    top_rate = max(t.hit_rate for t in pick.supporting_trends)
    total_hits = sum(t.hits for t in pick.supporting_trends)
    total_sample = sum(t.sample_size for t in pick.supporting_trends)

    units = pick.units_display

    home_handle = extract_handle(match.home)
    away_handle = extract_handle(match.away)

    # Show full team names so user can match to sportsbook,
    # with handles in bold for quick identification
    home_display = match.home if home_handle != match.home else home_handle
    away_display = match.away if away_handle != match.away else away_handle

    # Build odds string if real odds are attached
    odds_str = ""
    if pick.odds_line:
        from esporf.models import _parse_line

        parsed = _parse_line(pick.market)
        if parsed:
            direction = parsed[0]
            if direction.lower() == "over":
                odds_str = f"  ({pick.odds_line.over_american})"
            else:
                odds_str = f"  ({pick.odds_line.under_american})"

    edge_str = ""
    if pick.edge is not None and pick.edge > 0:
        edge_str = f"  |  **{pick.edge:.0%} edge**"

    lines = [
        f"### {home_display}  vs  {away_display}",
        f"### Kickoff: <t:{ts}:t>  (<t:{ts}:R>)",
        "",
        f"## {pick.market.upper()}  —  {units}{odds_str}",
        "",
        f"**{top_rate:.0%}** hit rate  ({total_hits}/{total_sample}){edge_str}",
    ]

    # Show match context: avg goals + offered lines
    context_parts = []
    if report.avg_goals is not None:
        context_parts.append(f"Matchup avg: **{report.avg_goals:.1f}** goals")
    if match.odds and match.odds.total_lines:
        offered = ", ".join(str(ol.line) for ol in match.odds.total_lines)
        context_parts.append(f"Lines offered: {offered}")
    if context_parts:
        lines.append("\n" + "  |  ".join(context_parts))

    color = _confidence_color(top_rate)

    embed = {
        "title": league_name,
        "description": "\n".join(lines),
        "color": color,
    }

    return embed


# ── Senders ──────────────────────────────────────────────────────


async def send_discord_alert(reports: list[MatchupReport]) -> None:
    """Send trend alerts to a Discord channel via webhook embeds."""
    url = settings.discord_webhook_url
    if not url:
        return

    for report in reports:
        if not report.has_trends:
            continue
        embed = _build_discord_embed(report)
        if not embed:
            continue
        payload = {"embeds": [embed]}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                logger.info("Discord alert sent for %s", report.match.display_name)
        except Exception as e:
            logger.warning("Failed to send Discord alert: %s", e)


async def send_telegram_alert(reports: list[MatchupReport]) -> None:
    """Send trend alerts to a Telegram chat."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    for report in reports:
        if not report.has_trends:
            continue
        match = report.match
        pick = report.best_bet
        if not pick:
            continue
        market = pick.market.upper()
        units = pick.units_display
        minutes = match.minutes_until
        time_str = _kickoff_est(match.start_time)
        time_detail = f"{time_str} ({minutes} min)" if minutes > 0 else f"{time_str} (LIVE)"

        top_rate = max(t.hit_rate for t in pick.supporting_trends)
        total_hits = sum(t.hits for t in pick.supporting_trends)
        total_sample = sum(t.sample_size for t in pick.supporting_trends)

        home_handle = extract_handle(match.home)
        away_handle = extract_handle(match.away)
        home_display = match.home if home_handle != match.home else home_handle
        away_display = match.away if away_handle != match.away else away_handle

        # Add odds if available
        odds_str = ""
        if pick.odds_line:
            from esporf.models import _parse_line

            parsed = _parse_line(pick.market)
            if parsed:
                direction = parsed[0]
                if direction.lower() == "over":
                    odds_str = f" ({pick.odds_line.over_american})"
                else:
                    odds_str = f" ({pick.odds_line.under_american})"

        edge_str = ""
        if pick.edge is not None and pick.edge > 0:
            edge_str = f" | {pick.edge:.0%} edge"

        avg_str = ""
        if report.avg_goals is not None:
            avg_str = f" | Avg: {report.avg_goals:.1f} goals"

        lines = [
            f"<b>{home_display} vs {away_display}</b>",
            f"<b>{market}{odds_str} — {units}</b>",
            time_detail,
            f"History: {total_hits}/{total_sample} ({top_rate:.0%}){edge_str}{avg_str}",
        ]

        msg = "\n".join(lines)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    api_url,
                    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                )
                resp.raise_for_status()
                logger.info("Telegram alert sent for %s", match.display_name)
        except Exception as e:
            logger.warning("Failed to send Telegram alert: %s", e)


async def send_alerts(reports: list[MatchupReport]) -> None:
    """Send alerts through all configured channels."""
    reports_with_trends = [r for r in reports if r.has_trends]
    if not reports_with_trends:
        return

    if settings.discord_webhook_url:
        await send_discord_alert(reports_with_trends)
    if settings.telegram_bot_token:
        await send_telegram_alert(reports_with_trends)
