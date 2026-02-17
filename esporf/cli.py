"""CLI entry point for the Esporf bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_time=True, show_path=False)],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="esporf",
        description="eSoccer betting data aggregator and edge finder",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # `esporf run` — start the continuous bot
    run_parser = subparsers.add_parser("run", help="Start the bot in continuous mode")
    run_parser.add_argument(
        "--interval",
        type=int,
        help="Override poll interval in seconds",
    )

    # `esporf scan` — run a single scan
    subparsers.add_parser("scan", help="Run a single scan and exit")

    # `esporf odds` — show current odds for all matches
    subparsers.add_parser("odds", help="Show current odds for all tracked matches")

    # `esporf stats` — show cached player stats
    subparsers.add_parser("stats", help="Show cached player statistics")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "run":
        from esporf.config import settings

        if args.interval:
            settings.poll_interval = args.interval
        from esporf.bot import run_bot

        asyncio.run(run_bot())

    elif args.command == "scan":
        from esporf.bot import scan_once

        asyncio.run(scan_once())

    elif args.command == "odds":
        asyncio.run(_show_odds())

    elif args.command == "stats":
        asyncio.run(_show_stats())


async def _show_odds() -> None:
    """Fetch and display current odds for all matches."""
    from rich.table import Table

    from esporf.sources.aggregator import DataAggregator

    agg = DataAggregator()
    try:
        with console.status("Fetching matches..."):
            matches = await agg.get_all_matches()

        if not matches:
            console.print("[yellow]No matches found.[/yellow]")
            return

        for em in matches:
            m = em.match
            table = Table(
                title=f"{m.display_name} ({'LIVE' if m.is_live else 'Upcoming'})",
                show_lines=True,
            )
            table.add_column("Market")
            table.add_column("Outcome")
            table.add_column("Book")
            table.add_column("Odds", justify="right")
            table.add_column("American", justify="right")
            table.add_column("Implied %", justify="right")

            for o in sorted(m.odds, key=lambda x: (x.market.value, x.outcome.value)):
                pick = o.outcome.value
                if o.line is not None:
                    pick += f" {o.line}"
                table.add_row(
                    o.market.value,
                    pick,
                    o.sportsbook,
                    f"{o.odds:.2f}",
                    o.american_odds(),
                    f"{o.implied_probability:.1%}",
                )

            console.print(table)
            console.print()
    finally:
        await agg.close()


async def _show_stats() -> None:
    """Show cached player stats from TotalCorner."""
    from rich.table import Table

    from esporf.sources.aggregator import DataAggregator

    agg = DataAggregator()
    try:
        with console.status("Fetching player stats..."):
            await agg.refresh_stats_cache()

        if not agg._stats_cache:
            console.print("[yellow]No stats available.[/yellow]")
            return

        table = Table(title="Player Statistics", show_lines=True)
        table.add_column("Player")
        table.add_column("P", justify="right")
        table.add_column("W", justify="right")
        table.add_column("D", justify="right")
        table.add_column("L", justify="right")
        table.add_column("Win %", justify="right")
        table.add_column("GF/G", justify="right")
        table.add_column("GA/G", justify="right")
        table.add_column("Avg Total", justify="right")

        for name, ps in sorted(
            agg._stats_cache.items(), key=lambda x: x[1].win_rate, reverse=True
        ):
            table.add_row(
                ps.name,
                str(ps.matches_played),
                str(ps.wins),
                str(ps.draws),
                str(ps.losses),
                f"{ps.win_rate:.0%}",
                f"{ps.avg_goals_scored:.1f}",
                f"{ps.avg_goals_conceded:.1f}",
                f"{ps.avg_total_goals:.1f}",
            )

        console.print(table)
    finally:
        await agg.close()


if __name__ == "__main__":
    main()
