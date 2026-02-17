"""Webhook-based alert delivery for trend signals (Discord, Telegram)."""

from __future__ import annotations

import logging

import httpx

from esporf.config import settings
from esporf.models import MatchupReport, Trend

logger = logging.getLogger(__name__)


def format_matchup_message(report: MatchupReport) -> str:
    """Format a matchup report into a readable alert message for Discord/Telegram."""
    match = report.match
    league = match.league
    league_name = league.display_name if league else f"League {match.league_id}"

    lines = [
        f"**{match.display_name}** — {league_name}",
        "",
    ]

    for trend in report.trends:
        emoji = _hit_rate_indicator(trend.hit_rate)
        lines.append(
            f"{emoji} **{trend.category}** — "
            f"{trend.record} ({trend.hit_rate_pct}) "
            f"[{_trend_type_short(trend.trend_type)}]"
        )
        if trend.recent_results:
            lines.append(f"   Recent: {', '.join(trend.recent_results[:5])}")

    lines.append("")
    lines.append(f"_Trends above {settings.min_hit_rate:.0%} over last {settings.last_n_matches} matches_")

    return "\n".join(lines)


def _hit_rate_indicator(rate: float) -> str:
    if rate >= 0.90:
        return ">>>>"
    elif rate >= 0.80:
        return ">>>"
    elif rate >= 0.75:
        return ">>"
    return ">"


def _trend_type_short(trend_type: str) -> str:
    return {
        "h2h": "H2H",
        "player_overall": "Overall",
        "player_home": "Home",
        "player_away": "Away",
    }.get(trend_type, trend_type)


async def send_discord_alert(reports: list[MatchupReport]) -> None:
    """Send trend alerts to a Discord channel via webhook."""
    url = settings.discord_webhook_url
    if not url:
        return

    for report in reports:
        if not report.has_trends:
            continue
        msg = format_matchup_message(report)
        # Discord has a 2000 char limit per message
        if len(msg) > 1900:
            msg = msg[:1900] + "\n..."
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json={"content": msg})
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

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for report in reports:
        if not report.has_trends:
            continue
        msg = format_matchup_message(report)
        # Telegram uses HTML — convert markdown bold
        msg = msg.replace("**", "<b>", 1)
        msg_parts = msg.split("**")
        html_msg = msg_parts[0]
        for i, part in enumerate(msg_parts[1:]):
            tag = "</b>" if i % 2 == 0 else "<b>"
            html_msg += tag + part

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={"chat_id": chat_id, "text": html_msg, "parse_mode": "HTML"},
                )
                resp.raise_for_status()
                logger.info("Telegram alert sent for %s", report.match.display_name)
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
