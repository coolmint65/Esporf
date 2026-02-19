"""Rich console output for displaying trends and matchup reports."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from esporf.models import MatchupReport

console = Console()

# ── Confidence bar helpers ───────────────────────────────────────

_CONF_STYLES = [
    (0.90, "bold green", "####"),
    (0.80, "green", "### "),
    (0.75, "yellow", "##  "),
    (0.00, "dim", "#   "),
]


def _confidence_bar(value: float) -> Text:
    for threshold, style, bar in _CONF_STYLES:
        if value >= threshold:
            return Text(bar, style=style)
    return Text("#   ", style="dim")


# ── Public display functions ─────────────────────────────────────


def display_matchup_report(report: MatchupReport) -> None:
    """Print the bet pick and supporting trends for a matchup."""
    match = report.match
    league_name = match.league.display_name if match.league else f"League {match.league_id}"
    kickoff = datetime.fromtimestamp(match.start_time).strftime("%H:%M")
    live_tag = " [bold red]LIVE[/]" if match.is_live else ""

    if not report.has_trends:
        console.print(
            f"  [dim]{kickoff}  {match.display_name} ({league_name}) — no bet[/dim]"
        )
        return

    # ── Bet pick banner ──
    pick = report.best_bet
    if pick:
        pick_panel = Panel(
            f"[bold white]{pick.market}[/]\n"
            f"[dim]{pick.reason}[/]",
            title=(
                f"[bold cyan]{match.display_name}[/]  "
                f"[dim]{league_name} | {kickoff}[/]{live_tag}"
            ),
            subtitle=f"[bold]Confidence: {pick.confidence_label} ({pick.confidence_pct})[/]",
            border_style="green" if pick.confidence >= 0.80 else "yellow",
            padding=(0, 2),
        )
        console.print(pick_panel)

    # ── Compact supporting trends table ──
    table = Table(
        show_header=True,
        show_lines=False,
        padding=(0, 1),
        expand=False,
    )
    table.add_column("", max_width=4)  # confidence bar
    table.add_column("Market", style="white", max_width=28)
    table.add_column("Src", max_width=8)
    table.add_column("Record", style="yellow", justify="center", max_width=7)
    table.add_column("%", justify="center", max_width=4)
    table.add_column("Recent", style="dim", max_width=22)

    for trend in report.trends[:8]:  # cap at 8 most relevant trends
        rate_style = "bold green" if trend.hit_rate >= 0.80 else "yellow"
        table.add_row(
            _confidence_bar(trend.hit_rate),
            trend.category,
            _trend_type_short(trend.trend_type),
            trend.record,
            Text(trend.hit_rate_pct, style=rate_style),
            " ".join(trend.recent_results[:3]),
        )

    console.print(table)
    console.print()


def display_scan_summary(
    total_matches: int,
    matches_with_trends: int,
    total_trends: int,
    db_total: int,
) -> None:
    """Print a compact summary after a scan cycle."""
    bets_color = "green" if matches_with_trends > 0 else "yellow"
    console.print(
        Panel(
            f"[{bets_color}]{matches_with_trends}[/] bet(s) found across "
            f"{total_matches} upcoming match(es)  |  "
            f"{total_trends} supporting trends  |  "
            f"DB: {db_total:,}",
            title="[bold]Scan Results[/]",
            border_style="blue",
            padding=(0, 1),
        )
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


def _trend_type_short(trend_type: str) -> str:
    labels = {
        "h2h": "[magenta]H2H[/]",
        "player_overall": "[cyan]All[/]",
        "player_home": "[green]Home[/]",
        "player_away": "[yellow]Away[/]",
    }
    return labels.get(trend_type, trend_type)
