"""Rich console output for displaying trends and matchup reports."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from esporf.models import MatchupReport, extract_handle, extract_team, league_display_name

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

    if not pick:
        has_odds = match.odds and match.odds.has_data
        if report.skip_reason:
            console.print(f"  [dim]{kickoff}  {match.display_name} — {report.skip_reason}[/dim]")
        elif report.has_trends and not has_odds:
            console.print(f"  [dim]{kickoff}  {match.display_name} — waiting for book odds[/dim]")
        elif report.has_trends:
            console.print(f"  [dim]{kickoff}  {match.display_name} — trends but no edge vs book[/dim]")
        else:
            console.print(f"  [dim]{kickoff}  {match.display_name} — no pick[/dim]")
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
    has_real_odds = pick.odds_line is not None or pick.spread_line is not None
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
    elif not pick.odds_line and not pick.spread_line:
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

    # Show form quality when it deviates from baseline
    if report.form_modifier >= 1.10:
        context_parts.append(f"[green]Form: {report.form_modifier:.2f}x[/green]")
    elif report.form_modifier <= 0.90:
        context_parts.append(f"[yellow]Form: {report.form_modifier:.2f}x[/yellow]")

    if context_parts:
        body += f"\n[dim]{' | '.join(context_parts)}[/dim]"

    home_handle = extract_handle(match.home)
    away_handle = extract_handle(match.away)
    home_team = extract_team(match.home)
    away_team = extract_team(match.away)

    home_display = f"[bold]{home_handle}[/] [dim]{home_team}[/]" if home_team else f"[bold]{home_handle}[/]"
    away_display = f"[bold]{away_handle}[/] [dim]{away_team}[/]" if away_team else f"[bold]{away_handle}[/]"

    title = (
        f"{home_display} vs {away_display}  "
        f"[dim]| {kickoff} ({time_tag})[/dim]"
    )

    console.print(Panel(body, title=title, border_style=color, padding=(0, 2)))


def display_scan_summary(
    total_matches: int,
    matches_with_picks: int,
    total_trends: int,
    db_total: int,
    league_db_counts: dict[int, int] | None = None,
) -> None:
    """One-line scan summary with optional per-league DB counts."""
    c = "green" if matches_with_picks > 0 else "yellow"
    console.print(
        f"  [{c}]{matches_with_picks}[/] odds-backed pick(s) from "
        f"{total_matches} match(es)  [dim]|  DB: {db_total:,}[/dim]"
    )
    if league_db_counts:
        parts = []
        for lid, count in sorted(league_db_counts.items()):
            name = league_display_name(lid)
            tag = "[green]" if count > 0 else "[red]"
            parts.append(f"{tag}{name}: {count:,}[/]")
        console.print(f"  [dim]{' | '.join(parts)}[/dim]")


def display_reports_by_league(reports: list[MatchupReport]) -> None:
    """Display reports grouped by league, with picks first and no-picks collapsed."""
    by_league: dict[int, list[MatchupReport]] = defaultdict(list)
    for r in reports:
        by_league[r.match.league_id].append(r)

    for lid in sorted(by_league):
        league_reports = by_league[lid]
        name = league_display_name(lid)
        picks = [r for r in league_reports if r.best_bet is not None]
        no_picks = [r for r in league_reports if r.best_bet is None]

        console.print(f"\n  [bold]{name}[/bold]  [dim]({len(league_reports)} match(es))[/dim]")

        # Show picks first (full detail)
        for report in picks:
            display_matchup_report(report)

        # Collapse no-pick matches into a compact summary
        if no_picks:
            _display_no_pick_summary(no_picks)


def _display_no_pick_summary(reports: list[MatchupReport]) -> None:
    """Show no-pick matches as a compact grouped summary instead of one line each."""
    # Group by reason
    by_reason: dict[str, list[MatchupReport]] = defaultdict(list)
    for r in reports:
        if r.skip_reason:
            by_reason[r.skip_reason].append(r)
        elif r.has_trends and not (r.match.odds and r.match.odds.has_data):
            by_reason["waiting for book odds"].append(r)
        elif r.has_trends:
            by_reason["trends but no edge vs book"].append(r)
        else:
            by_reason["no pick"].append(r)

    for reason, group in by_reason.items():
        if len(group) <= 2:
            # Few enough to show individually
            for r in group:
                kickoff = _kickoff_est(r.match.start_time)
                console.print(f"    [dim]{kickoff}  {r.match.display_name} — {reason}[/dim]")
        else:
            # Collapse into count
            console.print(f"    [dim]{len(group)} match(es) — {reason}[/dim]")


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
