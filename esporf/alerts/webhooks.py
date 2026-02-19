"""Webhook-based alert delivery for trend signals (Discord, Telegram)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from esporf.config import settings
from esporf.models import MatchupReport

logger = logging.getLogger(__name__)

# ── Formatting helpers ───────────────────────────────────────────

_EST = ZoneInfo("US/Eastern")


def _kickoff_est(ts: int) -> str:
    """Format a unix timestamp as '9:25 PM EST'."""
    dt = datetime.fromtimestamp(ts, tz=_EST)
    return dt.strftime("%-I:%M %p EST")


def _build_discord_embed(report: MatchupReport) -> dict:
    """Build a Discord embed matching the clean card style."""
    match = report.match
    pick = report.best_bet
    league = match.league
    league_name = league.display_name if league else f"League {match.league_id}"

    market_label = pick.market.upper() if pick else "NO PICK"
    minutes = match.minutes_until
    time_str = _kickoff_est(match.start_time)
    time_detail = f"{time_str} ({minutes} Minutes)" if minutes > 0 else f"{time_str} (LIVE)"

    # Aggregate history across supporting trends for the top-line stat
    if pick and pick.supporting_trends:
        total_hits = sum(t.hits for t in pick.supporting_trends)
        total_sample = sum(t.sample_size for t in pick.supporting_trends)
        top_rate = max(t.hit_rate for t in pick.supporting_trends)
        history_line = f"History: {total_hits}/{total_sample} ({top_rate:.1%})"
    else:
        history_line = ""

    # Color: green if high confidence, yellow/orange otherwise
    color = 0x2ECC71 if pick and pick.confidence >= 0.75 else 0xF1C40F

    description_parts = [
        f"**{league_name}**",
        "",
        f"**{match.home}**",
        "vs",
        f"**{match.away}**",
        "",
        f"**{market_label}**",
    ]
    if history_line:
        description_parts.append(f"_{history_line}_")

    embed = {
        "title": f"{match.home} vs {match.away} | {market_label}",
        "description": "\n".join(description_parts),
        "color": color,
        "footer": {"text": "Powered by Esporf"},
        "timestamp": datetime.fromtimestamp(match.start_time, tz=timezone.utc).isoformat(),
    }

    return embed


def _build_discord_content(report: MatchupReport) -> str:
    """One-line header above the embed."""
    match = report.match
    pick = report.best_bet
    market = pick.market.upper() if pick else "—"
    minutes = match.minutes_until
    time_str = _kickoff_est(match.start_time)
    time_detail = f"{time_str} ({minutes} Minutes)" if minutes > 0 else f"{time_str} (LIVE)"
    return f"**{match.home} vs {match.away} | {market}**\n{time_detail}"


# ── Senders ──────────────────────────────────────────────────────


async def send_discord_alert(reports: list[MatchupReport]) -> None:
    """Send trend alerts to a Discord channel via webhook embeds."""
    url = settings.discord_webhook_url
    if not url:
        return

    for report in reports:
        if not report.has_trends:
            continue
        content = _build_discord_content(report)
        embed = _build_discord_embed(report)
        payload = {"content": content, "embeds": [embed]}
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
        market = pick.market.upper() if pick else "—"
        minutes = match.minutes_until
        time_str = _kickoff_est(match.start_time)
        time_detail = f"{time_str} ({minutes} min)" if minutes > 0 else f"{time_str} (LIVE)"

        lines = [
            f"<b>{match.home} vs {match.away} | {market}</b>",
            time_detail,
        ]
        if pick and pick.supporting_trends:
            top_rate = max(t.hit_rate for t in pick.supporting_trends)
            total_hits = sum(t.hits for t in pick.supporting_trends)
            total_sample = sum(t.sample_size for t in pick.supporting_trends)
            lines.append(f"History: {total_hits}/{total_sample} ({top_rate:.1%})")

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
