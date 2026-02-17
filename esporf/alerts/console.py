"""Rich console output for displaying trends and matchup reports."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from esporf.models import MatchupReport, Trend

console = Console()


def display_matchup_report(report: MatchupReport) -> None:
    """Print all qualifying trends for a single matchup."""
    match = report.match
    league_name = match.league.display_name if match.league else f"League {match.league_id}"

    if not report.has_trends:
        console.print(
            f"  [dim]{match.display_name} ({league_name}) — no qualifying trends[/dim]"
        )
        return

    table = Table(
        title=f"[bold cyan]{match.display_name}[/bold cyan]  [dim]({league_name})[/dim]",
        show_lines=True,
        padding=(0, 1),
    )
    table.add_column("Trend", style="white", max_width=35)
    table.add_column("Type", style="blue", max_width=12)
    table.add_column("Record", style="yellow", justify="center")
    table.add_column("Hit Rate", justify="center")
    table.add_column("Recent Scores", style="dim", max_width=30)

    for trend in report.trends:
        rate_color = "bold green" if trend.hit_rate >= 0.80 else "bold yellow"
        table.add_row(
            trend.description.split(" in ")[0],  # short form
            _trend_type_label(trend.trend_type),
            trend.record,
            Text(trend.hit_rate_pct, style=rate_color),
            ", ".join(trend.recent_results[:5]),
        )

    console.print(table)


def display_scan_summary(
    total_matches: int,
    matches_with_trends: int,
    total_trends: int,
    db_total: int,
) -> None:
    """Print a summary after a scan cycle."""
    console.print(
        Panel(
            f"[bold]Scan Complete[/bold]\n"
            f"  Upcoming matches: {total_matches}\n"
            f"  Matches with trends: [{'green' if matches_with_trends > 0 else 'yellow'}]"
            f"{matches_with_trends}[/]\n"
            f"  Total qualifying trends: [{'green' if total_trends > 0 else 'yellow'}]"
            f"{total_trends}[/]\n"
            f"  Match history DB: {db_total:,} matches",
            title="Esporf Trend Scanner",
            border_style="blue",
        )
    )


def display_player_stats(player: str, matches: list) -> None:
    """Display a player's recent match history."""
    if not matches:
        console.print(f"[yellow]No matches found for {player}[/yellow]")
        return

    table = Table(title=f"Recent Matches: {player}", show_lines=True)
    table.add_column("Date", style="dim")
    table.add_column("Home")
    table.add_column("Score", justify="center", style="bold")
    table.add_column("Away")
    table.add_column("Total", justify="right")
    table.add_column("BTTS", justify="center")

    for m in matches[:20]:
        dt = datetime.fromtimestamp(m.start_time).strftime("%m/%d %H:%M")
        home_style = "bold green" if m.winner == m.home else ""
        away_style = "bold green" if m.winner == m.away else ""
        btts = "[green]Y[/]" if m.btts else "[red]N[/]"
        table.add_row(
            dt,
            Text(m.home, style=home_style),
            m.score_str(),
            Text(m.away, style=away_style),
            str(m.total_goals),
            btts,
        )

    console.print(table)


def _trend_type_label(trend_type: str) -> str:
    labels = {
        "h2h": "[magenta]H2H[/]",
        "player_overall": "[cyan]Overall[/]",
        "player_home": "[green]Home[/]",
        "player_away": "[yellow]Away[/]",
    }
    return labels.get(trend_type, trend_type)
