"""CLI entry point for the Esporf trend bot."""

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
        description="eSoccer trend finder — scrapes match history and surfaces "
        "high-confidence betting trends",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # `esporf run` — start the continuous bot
    run_parser = subparsers.add_parser("run", help="Start the bot in continuous mode")
    run_parser.add_argument(
        "--interval", type=int, help="Override poll interval in seconds"
    )

    # `esporf scan` — single scan and exit
    subparsers.add_parser("scan", help="Run a single scan and exit")

    # `esporf backfill` — populate the DB without scanning
    backfill_parser = subparsers.add_parser(
        "backfill", help="Fetch historical match data into the database"
    )
    backfill_parser.add_argument(
        "--pages", type=int, default=10, help="Number of pages to fetch per league"
    )

    # `esporf player` — look up a specific player
    player_parser = subparsers.add_parser(
        "player", help="Show a player's recent matches and trends"
    )
    player_parser.add_argument("name", help="Player name to look up")

    # `esporf h2h` — head-to-head lookup
    h2h_parser = subparsers.add_parser(
        "h2h", help="Show head-to-head history and trends between two players"
    )
    h2h_parser.add_argument("player_a", help="First player name")
    h2h_parser.add_argument("player_b", help="Second player name")

    # `esporf db` — database stats
    subparsers.add_parser("db", help="Show database statistics")

    # `esporf discord` — run as a Discord bot
    subparsers.add_parser(
        "discord", help="Run as a Discord bot with slash commands"
    )

    # `esporf api` — start the REST API server
    api_parser = subparsers.add_parser(
        "api", help="Start the REST API server (docs at /docs)"
    )
    api_parser.add_argument(
        "--host", type=str, default="0.0.0.0", help="Bind address (default: 0.0.0.0)"
    )
    api_parser.add_argument(
        "--port", type=int, default=8000, help="Port to listen on (default: 8000)"
    )

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

    elif args.command == "backfill":
        asyncio.run(_backfill(args.pages))

    elif args.command == "player":
        _player_lookup(args.name)

    elif args.command == "h2h":
        _h2h_lookup(args.player_a, args.player_b)

    elif args.command == "db":
        _db_stats()

    elif args.command == "discord":
        from esporf.discord_bot import run_discord_bot

        run_discord_bot()

    elif args.command == "api":
        import uvicorn

        console.print(
            f"[bold green]Starting Esporf API on {args.host}:{args.port}[/bold green]"
        )
        console.print(f"[dim]Docs: http://{args.host}:{args.port}/docs[/dim]")
        uvicorn.run("esporf.api:app", host=args.host, port=args.port, log_level="info")


async def _backfill(pages: int) -> None:
    from esporf.config import settings
    from esporf.database import MatchDatabase
    from esporf.sources.betsapi import BetsAPIClient

    api = BetsAPIClient()
    db = MatchDatabase()
    try:
        console.print(f"[bold]Backfilling {pages} pages per league...[/bold]")
        for lid in settings.tracked_league_ids:
            console.print(f"  League {lid}...")
            matches = await api.backfill_history(lid, pages=pages)
            added = db.insert_many(matches)
            console.print(f"    Fetched {len(matches)}, added {added} new")
        console.print(
            f"\n[bold green]Done. Total: {db.total_matches():,} matches in DB[/bold green]"
        )
    finally:
        await api.close()
        db.close()


def _player_lookup(name: str) -> None:
    from esporf.alerts.console import display_matchup_report, display_player_stats
    from esporf.analysis.trends import TrendAnalyzer
    from esporf.database import MatchDatabase
    from esporf.models import UpcomingMatch

    db = MatchDatabase()
    try:
        matches = db.get_player_matches(name, limit=20)
        if not matches:
            console.print(f"[yellow]No matches found for '{name}'[/yellow]")
            console.print("[dim]Player names are case-sensitive. Check spelling.[/dim]")
            players = db.get_all_players()
            close = [p for p in players if name.lower() in p.lower()]
            if close:
                console.print(f"[dim]Did you mean: {', '.join(close[:10])}?[/dim]")
            return

        display_player_stats(name, matches)

        # Also show overall trends
        analyzer = TrendAnalyzer(db)
        dummy_match = UpcomingMatch(
            match_id="lookup", league_id=0, home=name, away="(any)", start_time=0
        )
        report = analyzer.analyze_matchup(dummy_match)
        if report.has_trends:
            console.print(f"\n[bold]Qualifying trends for {name}:[/bold]")
            display_matchup_report(report)
    finally:
        db.close()


def _h2h_lookup(player_a: str, player_b: str) -> None:
    from esporf.alerts.console import display_matchup_report, display_player_stats
    from esporf.analysis.trends import TrendAnalyzer
    from esporf.database import MatchDatabase
    from esporf.models import UpcomingMatch

    db = MatchDatabase()
    try:
        matches = db.get_h2h_matches(player_a, player_b, limit=20)
        if not matches:
            console.print(
                f"[yellow]No H2H matches found between '{player_a}' and '{player_b}'[/yellow]"
            )
            return

        console.print(f"\n[bold]H2H: {player_a} vs {player_b} ({len(matches)} matches)[/bold]")
        display_player_stats(f"{player_a} vs {player_b}", matches)

        # Show H2H trends
        analyzer = TrendAnalyzer(db)
        dummy_match = UpcomingMatch(
            match_id="lookup",
            league_id=matches[0].league_id,
            home=player_a,
            away=player_b,
            start_time=0,
        )
        report = analyzer.analyze_matchup(dummy_match)
        if report.has_trends:
            console.print(f"\n[bold]Qualifying trends:[/bold]")
            display_matchup_report(report)
        else:
            console.print("[yellow]No trends meeting the threshold.[/yellow]")
    finally:
        db.close()


def _db_stats() -> None:
    from esporf.config import settings
    from esporf.database import MatchDatabase
    from esporf.models import league_display_name

    db = MatchDatabase()
    try:
        total = db.total_matches()
        console.print(f"\n[bold]Database: {db.db_path}[/bold]")
        console.print(f"  Total matches: {total:,}")
        for lid in settings.tracked_league_ids:
            count = db.total_matches_for_league(lid)
            name = league_display_name(lid)
            console.print(f"  {name}: {count:,}")
        players = db.get_all_players()
        console.print(f"  Unique players: {len(players)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
