"""Rich console output for displaying trends and matchup reports."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from esporf.models import MatchupReport, extract_handle

console = Console()

_EST = ZoneInfo("US/Eastern")


def _kickoff_est(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=_EST)
    return dt.strftime("%-I:%M %p")


def display_matchup_report(report: MatchupReport) -> None:
    """Print a single compact card per matchup."""
    match = report.match
    kickoff = _kickoff_est(match.start_time)
    minutes = match.minutes_until

    if not report.has_trends:
        console.print(f"  [dim]{kickoff}  {match.display_name} — no pick[/dim]")
        return

    pick = report.best_bet
    if not pick:
        return

    # Top-line history stat
    top_rate = max(t.hit_rate for t in pick.supporting_trends)
    total_hits = sum(t.hits for t in pick.supporting_trends)
    total_sample = sum(t.sample_size for t in pick.supporting_trends)
    history = f"{total_hits}/{total_sample} ({top_rate:.0%})"

    time_tag = f"in {minutes} min"
    if top_rate >= 0.81:
        color = "green"
    elif top_rate >= 0.70:
        color = "yellow"
    else:
        color = "bright_red"  # orange approximation in terminal

    units = pick.units_display

    body = (
        f"[bold white]{pick.market.upper()}  —  {units}[/]\n"
        f"[bold]{top_rate:.0%}[/] hit rate  ({total_hits}/{total_sample})"
    )
    home = extract_handle(match.home)
    away = extract_handle(match.away)

    title = (
        f"[bold]{home}[/] vs [bold]{away}[/]  "
        f"[dim]| {kickoff} ({time_tag})[/dim]"
    )

    console.print(Panel(body, title=title, border_style=color, padding=(0, 2)))


def display_scan_summary(
    total_matches: int,
    matches_with_trends: int,
    total_trends: int,
    db_total: int,
) -> None:
    """One-line scan summary."""
    c = "green" if matches_with_trends > 0 else "yellow"
    console.print(
        f"  [{c}]{matches_with_trends}[/] pick(s) from "
        f"{total_matches} match(es)  [dim]|  DB: {db_total:,}[/dim]"
    )


def display_player_stats(player: str, matches: list) -> None:
    """Display a player's recent match history."""
    if not matches:
        console.print(f"[yellow]No matches found for {player}[/yellow]")
        return

    table = Table(title=f"Recent Matches: {player}", show_lines=False)
    table.add_column("Date", style="dim")
    table.add_column("Home")
    table.add_column("Score", justify="center", style="bold")
    table.add_column("Away")

    for m in matches[:15]:
        dt = datetime.fromtimestamp(m.start_time, tz=_EST).strftime("%m/%d %-I:%M%p")
        home_style = "bold green" if m.winner == m.home else ""
        away_style = "bold green" if m.winner == m.away else ""
        table.add_row(
            dt,
            Text(m.home, style=home_style),
            m.score_str(),
            Text(m.away, style=away_style),
        )

    console.print(table)
