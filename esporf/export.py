"""Auto-updating CSV data export.

Writes human-readable CSV files to ``data/export/`` after every scan cycle.
Files are overwritten each time so they always reflect the latest state.
If the bot fails or the DB corrupts, these CSVs serve as a recovery snapshot.

Exported files:
- ``picks.csv``   — Full pick history with results, odds, P&L
- ``players.csv`` — Player form leaderboard with tiers and modifiers
- ``matches.csv`` — Raw match results (most recent 500 per league)
- ``summary.csv`` — Aggregate P&L by league and time period
"""

from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import league_display_name

logger = logging.getLogger(__name__)

EXPORT_DIR = os.path.join(settings.data_dir, "export")


def _ts_to_str(ts: int | None, tz: ZoneInfo | None = None) -> str:
    """Convert unix timestamp to human-readable string."""
    if not ts:
        return ""
    tz = tz or ZoneInfo(settings.timezone)
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M %Z")


def _ensure_dir() -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)


def _write_csv(filename: str, headers: list[str], rows: list[list]) -> None:
    """Write a CSV file atomically (write to tmp, then rename)."""
    path = os.path.join(EXPORT_DIR, filename)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    os.replace(tmp, path)
    logger.debug("Exported %s (%d rows)", filename, len(rows))


def export_picks(db: MatchDatabase) -> None:
    """Export full pick history with results and P&L."""
    tz = ZoneInfo(settings.timezone)
    picks = db.get_all_picks(limit=5000)

    headers = [
        "Date", "League", "Home", "Away", "Market", "Units",
        "Odds (Dec)", "Odds (US)", "Hit Rate", "Edge",
        "Result", "Score", "Profit (u)", "Resolved",
    ]

    rows = []
    for p in picks:
        # Convert decimal odds to american
        us_odds = ""
        if p.odds and p.odds > 0:
            if p.odds >= 2.0:
                us_odds = f"+{round((p.odds - 1) * 100)}"
            else:
                us_odds = f"{round(-100 / (p.odds - 1))}"

        score = ""
        if p.home_score is not None and p.away_score is not None:
            score = f"{p.home_score}-{p.away_score}"

        rows.append([
            _ts_to_str(p.created_at, tz),
            league_display_name(p.league_id),
            p.home,
            p.away,
            p.market,
            f"{p.units:.1f}",
            f"{p.odds:.2f}" if p.odds else "",
            us_odds,
            f"{p.hit_rate:.0%}",
            f"{p.edge:.0%}" if p.edge else "",
            p.result.value.upper(),
            score,
            f"{p.profit:+.2f}",
            _ts_to_str(p.resolved_at, tz),
        ])

    _write_csv("picks.csv", headers, rows)


def export_players(db: MatchDatabase) -> None:
    """Export player form leaderboard with tiers and modifiers."""
    tz = ZoneInfo(settings.timezone)

    headers = [
        "League", "Player", "Tier", "Matches", "W-L-D", "Win %",
        "GF/Game", "GA/Game", "O2.5 %", "O3.5 %", "O4.5 %", "O5.5 %",
        "Recent (10)", "Recent W-L-D", "Recent Win %", "Form Trend",
        "Modifier", "Last Updated",
    ]

    rows = []
    for lid in settings.tracked_league_ids:
        forms = db.get_all_player_forms(league_id=lid, min_matches=1)
        for f in forms:
            rows.append([
                league_display_name(lid),
                f.handle,
                f.tier.label,
                f.matches_played,
                f"{f.wins}-{f.losses}-{f.draws}",
                f"{f.win_rate:.1%}",
                f"{f.avg_goals_scored:.1f}",
                f"{f.avg_goals_conceded:.1f}",
                f"{f.over_2_5_rate:.0%}",
                f"{f.over_3_5_rate:.0%}",
                f"{f.over_4_5_rate:.0%}",
                f"{f.over_5_5_rate:.0%}",
                f.recent_matches,
                f"{f.recent_wins}-{f.recent_losses}-{f.recent_draws}",
                f"{f.recent_win_rate:.1%}",
                f.form_trend,
                f"{f.form_modifier:.2f}x",
                _ts_to_str(f.last_updated, tz),
            ])

    _write_csv("players.csv", headers, rows)


def export_matches(db: MatchDatabase) -> None:
    """Export recent match results (last 500 per league)."""
    tz = ZoneInfo(settings.timezone)

    headers = [
        "Date", "League", "Home", "Away",
        "Home Score", "Away Score", "Total Goals", "Result",
    ]

    rows = []
    for lid in settings.tracked_league_ids:
        matches = db.get_player_matches("", limit=500, league_id=lid)
        # get_player_matches with empty string won't work — use raw query
        conn = db._get_conn()
        raw = conn.execute(
            """SELECT * FROM matches WHERE league_id = ?
               ORDER BY start_time DESC LIMIT 500""",
            (lid,),
        ).fetchall()
        for m in raw:
            hs, aws = m["home_score"], m["away_score"]
            total = hs + aws
            if hs > aws:
                result = "Home Win"
            elif aws > hs:
                result = "Away Win"
            else:
                result = "Draw"

            rows.append([
                _ts_to_str(m["start_time"], tz),
                league_display_name(lid),
                m["home"],
                m["away"],
                hs,
                aws,
                total,
                result,
            ])

    _write_csv("matches.csv", headers, rows)


def export_summary(db: MatchDatabase) -> None:
    """Export aggregate P&L summary by league and time period."""
    now = int(time.time())
    day_ago = now - 86400
    week_ago = now - 604800
    month_ago = now - 2592000

    headers = [
        "League", "Period", "Picks", "Wins", "Losses", "Pushes",
        "Pending", "Win Rate", "Profit (u)", "Units Wagered", "ROI",
    ]

    rows = []

    periods = [
        ("All Time", None),
        ("Last 24h", day_ago),
        ("Last 7d", week_ago),
        ("Last 30d", month_ago),
    ]

    # Overall + per-league
    league_ids = [None] + settings.tracked_league_ids

    for lid in league_ids:
        league_name = "All Leagues" if lid is None else league_display_name(lid)
        for period_name, since in periods:
            s = db.get_pick_summary(league_id=lid, since=since)
            resolved = s["wins"] + s["losses"] + s["pushes"]
            wr = f"{s['wins'] / resolved:.1%}" if resolved else "N/A"
            roi = f"{s['profit'] / s['units_wagered']:.1%}" if s["units_wagered"] else "N/A"

            rows.append([
                league_name,
                period_name,
                s["total"],
                s["wins"],
                s["losses"],
                s["pushes"],
                s["pending"],
                wr,
                f"{s['profit']:+.2f}",
                f"{s['units_wagered']:.1f}",
                roi,
            ])

    _write_csv("summary.csv", headers, rows)


def export_all(db: MatchDatabase) -> None:
    """Run all exports. Called after every scan cycle."""
    _ensure_dir()
    try:
        export_picks(db)
        export_players(db)
        export_matches(db)
        export_summary(db)
        logger.info("CSV export complete → %s/", EXPORT_DIR)
    except Exception as e:
        logger.warning("CSV export failed: %s", e)
