"""Rich console output for displaying trends and matchup reports."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from esporf.models import MatchupReport, extract_handle

console = Console()

_EST = ZoneInfo("US/Eastern")

# Windows uses %#I for no-padding, Linux/macOS use %-I
_TIME_FMT = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
_DT_FMT = "%m/%d %#I:%M%p" if os.name == "nt" else "%m/%d %-I:%M%p"


def _kickoff_est(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=_EST)
    return dt.strftime(_TIME_FMT)


def display_matchup_report(report: MatchupReport) -> None:
    """Print a single compact card per matchup."""
    match = report.match
    kickoff = _kickoff_est(match.start_time)
    minutes = match.minutes_until

    pick = report.best_bet
    trend_pick = report.best_trend_pick if not pick else None

    if not pick and not trend_pick:
        if report.has_trends:
            console.print(f"  [dim]{kickoff}  {match.display_name} — trends but no alertable lines[/dim]")
        else:
            console.print(f"  [dim]{kickoff}  {match.display_name} — no pick[/dim]")
        return

    # Use trend_pick when no real odds pick is available
    if not pick and trend_pick:
        _display_trend_only_card(report, trend_pick, kickoff, minutes)
        return

    # Top-line history stat
    top_rate = max(t.hit_rate for t in pick.supporting_trends)
    total_hits = sum(t.hits for t in pick.supporting_trends)
    total_sample = sum(t.sample_size for t in pick.supporting_trends)

    time_tag = f"in {minutes} min"
    if top_rate >= 0.80:
        color = "green"
    elif top_rate >= 0.70:
        color = "yellow"
    else:
        color = "bright_red"  # orange approximation in terminal

    units = pick.units_display

    # Build body with actual odds and EV when available
    has_real_odds = pick.odds_line is not None
    odds_str = ""
    if pick.american_odds:
        odds_str = f"  [bold cyan]({pick.american_odds})[/]"

    # Only show edge and EV when we have real sportsbook odds
    edge_str = ""
    if has_real_odds and pick.edge is not None and pick.edge >= 0.01:
        edge_str = f"  |  [bold green]{pick.edge:.0%} edge[/]"

    ev_str = ""
    if has_real_odds:
        ev = pick.ev_per_unit
        if ev is not None:
            if ev > 0:
                ev_str = f"  |  [bold green]EV: +${ev:.2f}/u[/]"
            else:
                ev_str = f"  |  [bold red]EV: ${ev:.2f}/u[/]"

    juice_warn = ""
    if pick.is_heavy_juice:
        juice_warn = "\n[bold yellow]  ⚠ Heavy juice — low payout[/]"

    body = (
        f"[bold white]{pick.market.upper()}  —  {units}[/]{odds_str}\n"
        f"[bold]{top_rate:.0%}[/] hit rate  ({total_hits}/{total_sample}){edge_str}{ev_str}"
        f"{juice_warn}"
    )

    # Show match context: avg goals + available lines + sources
    context_parts: list[str] = []
    if report.avg_goals is not None:
        context_parts.append(f"Avg: {report.avg_goals:.1f} goals")
    if match.odds and match.odds.has_data:
        lines_display = "  ".join(f"{ol.line}" for ol in match.odds.total_lines)
        context_parts.append(f"Lines: {lines_display}")
    elif not pick.odds_line:
        context_parts.append("No book odds")

    # Show external data sources contributing to this pick
    src_types = {t.trend_type for t in pick.supporting_trends}
    ext_labels = []
    if "tc_player" in src_types:
        ext_labels.append("TotalCorner")
    if "forebet" in src_types:
        ext_labels.append("Forebet")
    if ext_labels:
        context_parts.append(f"+ {', '.join(ext_labels)}")

    if context_parts:
        body += f"\n[dim]{' | '.join(context_parts)}[/dim]"

    home_display = extract_handle(match.home)
    away_display = extract_handle(match.away)

    title = (
        f"[bold]{home_display}[/] vs [bold]{away_display}[/]  "
        f"[dim]| {kickoff} ({time_tag})[/dim]"
    )

    console.print(Panel(body, title=title, border_style=color, padding=(0, 2)))


def _display_trend_only_card(
    report: MatchupReport,
    pick: "BetPick",
    kickoff: str,
    minutes: int,
) -> None:
    """Display a trend-only card when no sportsbook odds are available yet."""
    match = report.match
    top_rate = max(t.hit_rate for t in pick.supporting_trends)
    total_hits = sum(t.hits for t in pick.supporting_trends)
    total_sample = sum(t.sample_size for t in pick.supporting_trends)

    time_tag = f"in {minutes} min"
    color = "green" if top_rate >= 0.80 else "yellow" if top_rate >= 0.70 else "bright_red"

    body = (
        f"[bold white]{pick.market.upper()}[/]  [dim](no book odds yet)[/dim]\n"
        f"[bold]{top_rate:.0%}[/] hit rate  ({total_hits}/{total_sample})"
    )

    # Show match context
    context_parts: list[str] = []
    if report.avg_goals is not None:
        context_parts.append(f"Avg: {report.avg_goals:.1f} goals")

    src_types = {t.trend_type for t in pick.supporting_trends}
    ext_labels = []
    if "tc_player" in src_types:
        ext_labels.append("TotalCorner")
    if "forebet" in src_types:
        ext_labels.append("Forebet")
    if ext_labels:
        context_parts.append(f"+ {', '.join(ext_labels)}")

    body += f"\n[dim]{' | '.join(context_parts)}[/dim]"

    home_display = extract_handle(match.home)
    away_display = extract_handle(match.away)

    title = (
        f"[bold]{home_display}[/] vs [bold]{away_display}[/]  "
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
        dt = datetime.fromtimestamp(m.start_time, tz=_EST).strftime(_DT_FMT)
        home_style = "bold green" if m.winner == m.home else ""
        away_style = "bold green" if m.winner == m.away else ""
        table.add_row(
            dt,
            Text(m.home, style=home_style),
            m.score_str(),
            Text(m.away, style=away_style),
        )

    console.print(table)
