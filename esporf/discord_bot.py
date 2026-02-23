"""Discord bot frontend for Esporf — replaces webhook alerts with a proper bot.

Runs the same scan loop as `esporf run` but inside a discord.py background task,
sending picks as rich embeds to a configured channel.  Adds slash commands for
on-demand lookups (/stats, /picks, /leagues).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from esporf.alerts.webhooks import _build_discord_embed, _confidence_color
from esporf.bot import EsporfBot, _match_key
from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import MatchupReport, PickResult, TrackedPick, extract_handle, league_display_name

logger = logging.getLogger(__name__)

_EST = ZoneInfo("US/Eastern")


# ── Bot class ────────────────────────────────────────────────────


class EsporfDiscordBot(discord.Client):
    """Discord bot that wraps the core EsporfBot scan loop."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="esoccer odds",
        )
        super().__init__(intents=intents, activity=activity, status=discord.Status.online)
        self.tree = app_commands.CommandTree(self)
        self.scanner = EsporfBot(skip_webhook_alerts=True)
        self._alert_channel: discord.TextChannel | None = None
        self._scan_count = 0
        self._latest_reports: list[MatchupReport] = []

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup_hook(self) -> None:
        """Called once the bot is logged in — register commands & start loop."""
        _register_commands(self)
        await self.tree.sync()
        logger.info("Slash commands synced")

        # Backfill history before first scan
        await self.scanner.backfill()

        # Start the scan loop with the configured interval
        interval = settings.poll_interval
        self.scan_loop.change_interval(seconds=interval)
        self.scan_loop.start()
        logger.info("Scan loop started (every %ds)", interval)

    async def on_ready(self) -> None:
        channel_id = settings.discord_channel_id
        if channel_id:
            cid = int(channel_id)
            # Try cache first, then fall back to an API call.
            # get_channel can return None when the guild cache hasn't
            # been populated yet (common on first READY).
            ch = self.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.fetch_channel(cid)
                except discord.NotFound:
                    ch = None
                except discord.Forbidden:
                    logger.error(
                        "Bot lacks permission to access channel %s — "
                        "check the bot's role in your server",
                        channel_id,
                    )
                    ch = None
            if ch and isinstance(ch, discord.TextChannel):
                self._alert_channel = ch
                logger.info("Alert channel: #%s (%s)", ch.name, ch.id)
            else:
                logger.warning(
                    "Could not find text channel %s — alerts disabled", channel_id
                )
        else:
            logger.warning("No DISCORD_CHANNEL_ID set — alerts disabled")

        logger.info("Logged in as %s (id=%s)", self.user, self.user.id)

    # ── Scan loop ─────────────────────────────────────────────

    @tasks.loop(seconds=180)  # default; overridden in setup_hook
    async def scan_loop(self) -> None:
        self._scan_count += 1
        logger.info("Discord scan #%d starting", self._scan_count)
        try:
            reports = await self.scanner.scan_once()
            self._latest_reports = reports or []
            await self._send_alerts(reports or [])
        except Exception as e:
            logger.error("Scan #%d failed: %s", self._scan_count, e, exc_info=True)

    @scan_loop.before_loop
    async def _before_scan(self) -> None:
        await self.wait_until_ready()

    # ── Alert delivery ────────────────────────────────────────

    async def _send_alerts(self, reports: list[MatchupReport]) -> None:
        """Send new odds-backed picks to the alert channel."""
        if not self._alert_channel:
            return

        tracked = set(settings.tracked_league_ids)
        new_reports = [
            r for r in reports
            if r.best_bet is not None
            and r.match.league_id in tracked
            and _match_key(r.match) not in self.scanner._alerted_keys
        ]

        for report in new_reports:
            embed_data = _build_discord_embed(report)
            if not embed_data:
                continue

            embed = discord.Embed(
                title=embed_data.get("title", ""),
                description=embed_data.get("description", ""),
                color=embed_data.get("color", 0x95A5A6),
            )

            content = ""
            if settings.discord_role_id:
                content = f"<@&{settings.discord_role_id}>"

            try:
                await self._alert_channel.send(content=content or None, embed=embed)
                self.scanner._alerted_keys.add(_match_key(report.match))
                self.scanner.record_pick(report)
                logger.info("Alert sent for %s", report.match.display_name)
            except Exception as e:
                logger.warning("Failed to send alert: %s", e)

    # ── Cleanup ───────────────────────────────────────────────

    async def close(self) -> None:
        self.scan_loop.cancel()
        await self.scanner.api.close()
        await self.scanner.esb.close()
        await self.scanner.ace.close()
        await self.scanner.kambi.close()
        await self.scanner.tc.close()
        await self.scanner.forebet.close()
        self.scanner.db.close()
        await super().close()


# ── Slash commands ────────────────────────────────────────────


def _register_commands(bot: EsporfDiscordBot) -> None:
    """Register all slash commands on the bot's command tree."""

    @bot.tree.command(name="stats", description="Show database and bot statistics")
    async def cmd_stats(interaction: discord.Interaction) -> None:
        db = bot.scanner.db
        total = db.total_matches()

        lines = [f"**Database:** {total:,} total matches"]
        for lid in settings.tracked_league_ids:
            count = db.total_matches_for_league(lid)
            name = league_display_name(lid)
            lines.append(f"  {name}: {count:,}")

        players = db.get_all_players()
        lines.append(f"  Unique players: {len(players)}")
        lines.append("")
        lines.append(f"**Scans completed:** {bot._scan_count}")
        lines.append(f"**Alerts sent:** {len(bot.scanner._alerted_keys)}")
        lines.append(f"**Poll interval:** {settings.poll_interval}s")

        embed = discord.Embed(
            title="Esporf Stats",
            description="\n".join(lines),
            color=0x3498DB,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="picks", description="Show the latest picks from the most recent scan")
    async def cmd_picks(interaction: discord.Interaction) -> None:
        reports = bot._latest_reports
        if not reports:
            await interaction.response.send_message(
                "No picks from the latest scan. Waiting for next cycle...",
                ephemeral=True,
            )
            return

        embeds = []
        for report in reports:
            embed_data = _build_discord_embed(report)
            if not embed_data:
                continue
            embeds.append(
                discord.Embed(
                    title=embed_data.get("title", ""),
                    description=embed_data.get("description", ""),
                    color=embed_data.get("color", 0x95A5A6),
                )
            )

        if not embeds:
            await interaction.response.send_message(
                "Latest scan had matches but no odds-backed picks.",
                ephemeral=True,
            )
            return

        # Discord allows up to 10 embeds per message
        await interaction.response.send_message(
            content=f"**{len(embeds)} pick(s) from latest scan:**",
            embeds=embeds[:10],
        )

    @bot.tree.command(name="leagues", description="Show tracked leagues and their status")
    async def cmd_leagues(interaction: discord.Interaction) -> None:
        db = bot.scanner.db
        lines = []
        for lid in settings.tracked_league_ids:
            name = league_display_name(lid)
            count = db.total_matches_for_league(lid)
            lines.append(f"**{name}** (ID: {lid}) — {count:,} matches in DB")

        embed = discord.Embed(
            title="Tracked Leagues",
            description="\n".join(lines),
            color=0x2ECC71,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="record", description="Show betting record, profit, and ROI")
    @app_commands.describe(league="Filter by league (optional)")
    @app_commands.choices(league=[
        app_commands.Choice(name="All Leagues", value=0),
        app_commands.Choice(name="GG League", value=42648),
        app_commands.Choice(name="GT Leagues", value=42649),
        app_commands.Choice(name="Volta", value=38439),
    ])
    async def cmd_record(
        interaction: discord.Interaction,
        league: app_commands.Choice[int] | None = None,
    ) -> None:
        db = bot.scanner.db
        league_id = league.value if league and league.value != 0 else None
        summary = db.get_pick_summary(league_id=league_id)

        wins = summary["wins"]
        losses = summary["losses"]
        pushes = summary["pushes"]
        pending = summary["pending"]
        total = summary["total"]
        profit = summary["profit"]
        wagered = summary["units_wagered"]

        if total == 0 and pending == 0:
            await interaction.response.send_message(
                "No picks tracked yet. Picks are recorded when alerts are sent.",
                ephemeral=True,
            )
            return

        roi = (profit / wagered * 100) if wagered > 0 else 0.0
        profit_sign = "+" if profit >= 0 else ""

        title = "Betting Record"
        if league and league.value != 0:
            title += f" — {league.name}"

        lines = [
            f"## {wins}W - {losses}L" + (f" - {pushes}P" if pushes else ""),
            "",
            f"**Profit:** {profit_sign}{profit:.2f}u",
            f"**ROI:** {profit_sign}{roi:.1f}%",
            f"**Units Wagered:** {wagered:.1f}u",
        ]

        if pending:
            lines.append(f"**Pending:** {pending} pick(s)")

        # Win rate
        if total > 0:
            win_rate = wins / total * 100
            lines.append(f"**Win Rate:** {win_rate:.1f}%")

        color = 0x2ECC71 if profit >= 0 else 0xED4245  # green if profitable, red if not

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=color,
        )
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="results", description="Show recent pick results")
    @app_commands.describe(count="Number of recent picks to show (default 10)")
    async def cmd_results(
        interaction: discord.Interaction,
        count: int = 10,
    ) -> None:
        db = bot.scanner.db
        count = min(max(count, 1), 25)  # clamp between 1-25
        picks = db.get_all_picks(limit=count)

        if not picks:
            await interaction.response.send_message(
                "No picks tracked yet.", ephemeral=True,
            )
            return

        lines = []
        for p in picks:
            home_h = extract_handle(p.home)
            away_h = extract_handle(p.away)
            score = p.score_str or "pending"
            emoji = p.result_emoji
            league_name = league_display_name(p.league_id)

            odds_str = f" @ {p.odds:.2f}" if p.odds else ""
            profit_str = f" ({p.profit_display})" if p.is_resolved else ""

            lines.append(
                f"{emoji} **{home_h}** vs **{away_h}** [{score}]\n"
                f"\u2003{p.market} {p.units:.1f}u{odds_str}{profit_str}\n"
                f"\u2003*{league_name}* — <t:{p.start_time}:d>"
            )

        embed = discord.Embed(
            title=f"Recent Picks ({len(picks)})",
            description="\n\n".join(lines),
            color=0x3498DB,
        )

        # Add summary footer
        summary = db.get_pick_summary()
        total = summary["total"]
        if total > 0:
            profit = summary["profit"]
            sign = "+" if profit >= 0 else ""
            embed.set_footer(
                text=f"Overall: {summary['wins']}W-{summary['losses']}L | {sign}{profit:.2f}u profit"
            )

        await interaction.response.send_message(embed=embed)


# ── Entry point ───────────────────────────────────────────────


def run_discord_bot() -> None:
    """Launch the Discord bot (blocking)."""
    token = settings.discord_bot_token
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Add it to your .env file.\n"
            "Create a bot at https://discord.com/developers/applications"
        )

    bot = EsporfDiscordBot()
    bot.run(token, log_handler=None)  # logging already configured by CLI
