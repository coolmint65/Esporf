"""Webhook-based alert delivery (Discord, Telegram)."""

from __future__ import annotations

import logging

import httpx

from esporf.config import settings
from esporf.models import Edge

logger = logging.getLogger(__name__)


def format_edge_message(edge: Edge) -> str:
    """Format an edge into a readable alert message."""
    league = edge.match.league
    league_name = league.display_name if league else f"League {edge.match.league_id}"

    pick = edge.outcome.value
    if edge.line is not None:
        pick += f" {edge.line}"

    lines = [
        f"**EDGE FOUND** | {edge.edge_type.upper()}",
        f"Match: {edge.match.display_name}",
        f"League: {league_name}",
        f"Pick: {pick} @ {edge.best_odds:.2f} ({edge.best_book})",
        f"Fair: {edge.fair_odds:.2f} | Edge: +{edge.edge_percent:.1f}%",
        f"Details: {edge.details}",
    ]
    return "\n".join(lines)


async def send_discord_alert(edges: list[Edge]) -> None:
    """Send edge alerts to a Discord channel via webhook."""
    url = settings.discord_webhook_url
    if not url:
        return

    for edge in edges:
        msg = format_edge_message(edge)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={"content": msg},
                )
                resp.raise_for_status()
                logger.info("Discord alert sent for %s", edge.match.display_name)
        except Exception as e:
            logger.warning("Failed to send Discord alert: %s", e)


async def send_telegram_alert(edges: list[Edge]) -> None:
    """Send edge alerts to a Telegram chat."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for edge in edges:
        msg = format_edge_message(edge)
        # Convert markdown bold to Telegram HTML bold
        msg = msg.replace("**", "<b>").replace("</b><b>", "**")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": msg,
                        "parse_mode": "HTML",
                    },
                )
                resp.raise_for_status()
                logger.info("Telegram alert sent for %s", edge.match.display_name)
        except Exception as e:
            logger.warning("Failed to send Telegram alert: %s", e)


async def send_alerts(edges: list[Edge]) -> None:
    """Send alerts through all configured channels."""
    if not edges:
        return

    if settings.discord_webhook_url:
        await send_discord_alert(edges)
    if settings.telegram_bot_token:
        await send_telegram_alert(edges)
