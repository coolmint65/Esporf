"""Rich console output for displaying edges and match data."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from esporf.models import Edge, MarketType

console = Console()


def display_edges(edges: list[Edge]) -> None:
    """Print detected edges in a formatted table."""
    if not edges:
        console.print(
            Panel(
                "[yellow]No edges found above threshold.[/yellow]\n"
                "Try lowering MIN_EDGE_PERCENT or waiting for more data.",
                title="Edge Scanner",
            )
        )
        return

    table = Table(
        title=f"[bold green]Found {len(edges)} Edge(s)[/bold green]",
        show_lines=True,
        padding=(0, 1),
    )
    table.add_column("Match", style="cyan", max_width=30)
    table.add_column("League", style="blue", max_width=20)
    table.add_column("Type", style="yellow")
    table.add_column("Market", style="white")
    table.add_column("Pick", style="bold white")
    table.add_column("Book", style="magenta")
    table.add_column("Odds", style="green", justify="right")
    table.add_column("Fair", style="dim", justify="right")
    table.add_column("Edge %", style="bold green", justify="right")
    table.add_column("Details", style="dim", max_width=50)

    for edge in edges:
        league_name = ""
        if edge.match.league:
            league_name = edge.match.league.display_name
        else:
            league_name = f"League {edge.match.league_id}"

        pick = edge.outcome.value
        if edge.line is not None:
            pick += f" {edge.line}"

        edge_color = "green" if edge.edge_percent >= 5 else "yellow"

        table.add_row(
            edge.match.display_name,
            league_name,
            _edge_type_label(edge.edge_type),
            edge.market.value,
            pick,
            edge.best_book,
            f"{edge.best_odds:.2f}",
            f"{edge.fair_odds:.2f}",
            Text(f"+{edge.edge_percent:.1f}%", style=f"bold {edge_color}"),
            edge.details[:50],
        )

    console.print(table)


def display_scan_summary(
    total_matches: int,
    total_edges: int,
    leagues_scanned: int,
) -> None:
    """Print a summary of the scan results."""
    console.print(
        Panel(
            f"[bold]Scan Complete[/bold]\n"
            f"  Leagues: {leagues_scanned}\n"
            f"  Matches: {total_matches}\n"
            f"  Edges found: [{'green' if total_edges > 0 else 'yellow'}]{total_edges}[/]",
            title="eSoccer Edge Scanner",
            border_style="blue",
        )
    )


def display_match_odds(match_name: str, odds_data: dict) -> None:
    """Display all odds for a single match in a readable format."""
    table = Table(title=f"Odds: {match_name}", show_lines=True)
    table.add_column("Market")
    table.add_column("Outcome")
    table.add_column("Book")
    table.add_column("Odds", justify="right")
    table.add_column("Implied %", justify="right")

    for (market, outcome), books in sorted(odds_data.items()):
        for book_name, odds_line in books.items():
            table.add_row(
                market,
                outcome,
                book_name,
                f"{odds_line.odds:.2f}",
                f"{odds_line.implied_probability:.1%}",
            )

    console.print(table)


def _edge_type_label(edge_type: str) -> str:
    labels = {
        "line_discrepancy": "[yellow]Line Gap[/]",
        "steam_move": "[red]Steam[/]",
        "clv": "[blue]CLV[/]",
        "model_edge": "[green]Model[/]",
    }
    return labels.get(edge_type, edge_type)
