"""REST API for Esporf — exposes picks, stats, players, and match data.

Run with `esporf api` or directly: `uvicorn esporf.api:app`
Interactive docs available at /docs (Swagger UI).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import (
    League,
    PickResult,
    extract_handle,
    league_display_name,
)

app = FastAPI(
    title="Esporf API",
    description="eSoccer betting trend data — picks, stats, players, and match history.",
    version="1.0.0",
)


def _get_db() -> MatchDatabase:
    return MatchDatabase()


# ── Response schemas ────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str
    version: str
    total_matches: int
    total_picks: int
    uptime_note: str


class PickResponse(BaseModel):
    id: int | None
    match_id: str
    league: str
    league_id: int
    home: str
    away: str
    start_time: int
    start_time_fmt: str
    market: str
    units: float
    odds: float | None
    odds_american: str | None
    hit_rate: float
    hit_rate_pct: str
    edge: float | None
    edge_pct: str | None
    result: str
    profit: float
    profit_display: str
    score: str | None
    created_at: int


class StatsResponse(BaseModel):
    record: str
    wins: int
    losses: int
    pushes: int
    pending: int
    total_decided: int
    win_rate: float | None
    win_rate_pct: str
    profit: float
    profit_display: str
    units_wagered: float
    roi: float | None
    roi_pct: str


class LeagueStatsResponse(BaseModel):
    league: str
    league_id: int
    record: str
    wins: int
    losses: int
    profit: float
    profit_display: str
    win_rate: float | None
    win_rate_pct: str
    total_matches: int


class BreakdownResponse(BaseModel):
    overall: StatsResponse
    by_league: list[LeagueStatsResponse]
    by_market: list[dict]
    by_confidence: list[dict]


class PlayerResponse(BaseModel):
    handle: str
    league: str
    league_id: int
    tier: str
    form_trend: str
    form_modifier: float
    matches_played: int
    wins: int
    losses: int
    draws: int
    win_rate: float
    win_rate_pct: str
    goals_scored: int
    goals_conceded: int
    avg_goals_scored: float
    avg_goals_conceded: float
    avg_total_goals: float
    over_rates: dict[str, float]
    recent: dict


class MatchResponse(BaseModel):
    match_id: str
    league: str
    league_id: int
    home: str
    away: str
    home_score: int
    away_score: int
    total_goals: int
    score: str
    winner: str | None
    start_time: int
    start_time_fmt: str


class H2HResponse(BaseModel):
    player_a: str
    player_b: str
    total_matches: int
    matches: list[MatchResponse]
    summary: dict


class ScanStatusResponse(BaseModel):
    total_scans: int
    last_scan: dict | None
    database: dict


# ── Helpers ─────────────────────────────────────────────────────


def _fmt_time(ts: int) -> str:
    """Format a unix timestamp as a human-readable string."""
    if ts <= 0:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _decimal_to_american(dec: float | None) -> str | None:
    if dec is None:
        return None
    if dec >= 2.0:
        return f"+{(dec - 1) * 100:.0f}"
    elif dec > 1.0:
        return f"{-100 / (dec - 1):.0f}"
    return "N/A"


def _build_stats(summary: dict) -> StatsResponse:
    wins = summary["wins"]
    losses = summary["losses"]
    pushes = summary["pushes"]
    pending = summary["pending"]
    total = summary["total"]
    profit = summary["profit"]
    units_wagered = summary["units_wagered"]

    win_rate = wins / total if total > 0 else None
    roi = profit / units_wagered if units_wagered > 0 else None

    return StatsResponse(
        record=f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
        wins=wins,
        losses=losses,
        pushes=pushes,
        pending=pending,
        total_decided=total,
        win_rate=win_rate,
        win_rate_pct=f"{win_rate:.1%}" if win_rate is not None else "N/A",
        profit=round(profit, 2),
        profit_display=f"{'+' if profit >= 0 else ''}{profit:.1f}u",
        units_wagered=round(units_wagered, 2),
        roi=round(roi, 4) if roi is not None else None,
        roi_pct=f"{roi:.1%}" if roi is not None else "N/A",
    )


def _pick_to_response(pick) -> PickResponse:
    score = None
    if pick.home_score is not None and pick.away_score is not None:
        score = f"{pick.home_score}-{pick.away_score}"

    return PickResponse(
        id=pick.id,
        match_id=pick.match_id,
        league=league_display_name(pick.league_id),
        league_id=pick.league_id,
        home=pick.home,
        away=pick.away,
        start_time=pick.start_time,
        start_time_fmt=_fmt_time(pick.start_time),
        market=pick.market,
        units=pick.units,
        odds=pick.odds,
        odds_american=_decimal_to_american(pick.odds),
        hit_rate=pick.hit_rate,
        hit_rate_pct=f"{pick.hit_rate:.0%}",
        edge=pick.edge,
        edge_pct=f"{pick.edge:.0%}" if pick.edge is not None else None,
        result=pick.result.value,
        profit=round(pick.profit, 2),
        profit_display=f"{'+' if pick.profit >= 0 else ''}{pick.profit:.2f}u",
        score=score,
        created_at=pick.created_at,
    )


def _match_to_response(m) -> MatchResponse:
    return MatchResponse(
        match_id=m.match_id,
        league=league_display_name(m.league_id),
        league_id=m.league_id,
        home=m.home,
        away=m.away,
        home_score=m.home_score,
        away_score=m.away_score,
        total_goals=m.total_goals,
        score=m.score_str(),
        winner=m.winner,
        start_time=m.start_time,
        start_time_fmt=_fmt_time(m.start_time),
    )


# ── Routes ──────────────────────────────────────────────────────


@app.get("/", response_model=HealthResponse)
def health():
    """API health check and basic info."""
    db = _get_db()
    try:
        total_matches = db.total_matches()
        summary = db.get_pick_summary()
        return HealthResponse(
            status="ok",
            version="1.0.0",
            total_matches=total_matches,
            total_picks=summary["total"] + summary["pending"],
            uptime_note="Esporf API is running",
        )
    finally:
        db.close()


@app.get("/stats", response_model=StatsResponse)
def get_stats(
    league_id: int | None = Query(None, description="Filter by league ID"),
    days: int | None = Query(None, description="Only include picks from the last N days"),
):
    """Overall record, units, win rate, and ROI."""
    db = _get_db()
    try:
        since = int(time.time()) - (days * 86400) if days else None
        summary = db.get_pick_summary(league_id=league_id, since=since)
        return _build_stats(summary)
    finally:
        db.close()


@app.get("/stats/breakdown", response_model=BreakdownResponse)
def get_stats_breakdown():
    """Full breakdown by league, by market, and by confidence tier."""
    db = _get_db()
    try:
        # Overall
        overall_summary = db.get_pick_summary()
        overall = _build_stats(overall_summary)

        # By league
        by_league = []
        for league in League:
            lid = league.value
            s = db.get_pick_summary(league_id=lid)
            total = s["total"]
            win_rate = s["wins"] / total if total > 0 else None
            by_league.append(LeagueStatsResponse(
                league=league.display_name,
                league_id=lid,
                record=f"{s['wins']}-{s['losses']}" + (f"-{s['pushes']}" if s["pushes"] else ""),
                wins=s["wins"],
                losses=s["losses"],
                profit=round(s["profit"], 2),
                profit_display=f"{'+' if s['profit'] >= 0 else ''}{s['profit']:.1f}u",
                win_rate=win_rate,
                win_rate_pct=f"{win_rate:.1%}" if win_rate is not None else "N/A",
                total_matches=db.total_matches_for_league(lid),
            ))

        # By market (group picks by market type)
        all_picks = db.get_all_picks(limit=10000)
        market_buckets: dict[str, dict] = {}
        conf_buckets: dict[str, dict] = {}

        for p in all_picks:
            if p.result == PickResult.PENDING:
                continue

            # Market grouping
            market_key = _classify_market(p.market)
            if market_key not in market_buckets:
                market_buckets[market_key] = {"wins": 0, "losses": 0, "pushes": 0, "profit": 0.0}
            b = market_buckets[market_key]
            if p.result == PickResult.WIN:
                b["wins"] += 1
            elif p.result == PickResult.LOSS:
                b["losses"] += 1
            elif p.result == PickResult.PUSH:
                b["pushes"] += 1
            b["profit"] += p.profit

            # Confidence tier grouping
            tier_label = _classify_confidence(p.units)
            if tier_label not in conf_buckets:
                conf_buckets[tier_label] = {
                    "wins": 0, "losses": 0, "pushes": 0, "profit": 0.0, "units_key": p.units,
                }
            c = conf_buckets[tier_label]
            if p.result == PickResult.WIN:
                c["wins"] += 1
            elif p.result == PickResult.LOSS:
                c["losses"] += 1
            elif p.result == PickResult.PUSH:
                c["pushes"] += 1
            c["profit"] += p.profit

        by_market = []
        for mkt, b in sorted(market_buckets.items()):
            total = b["wins"] + b["losses"] + b["pushes"]
            wr = b["wins"] / total if total > 0 else None
            by_market.append({
                "market": mkt,
                "record": f"{b['wins']}-{b['losses']}" + (f"-{b['pushes']}" if b["pushes"] else ""),
                "wins": b["wins"],
                "losses": b["losses"],
                "profit": round(b["profit"], 2),
                "profit_display": f"{'+' if b['profit'] >= 0 else ''}{b['profit']:.1f}u",
                "win_rate_pct": f"{wr:.1%}" if wr is not None else "N/A",
            })

        by_confidence = []
        for tier, c in sorted(conf_buckets.items(), key=lambda x: -x[1].get("units_key", 0)):
            total = c["wins"] + c["losses"] + c["pushes"]
            wr = c["wins"] / total if total > 0 else None
            by_confidence.append({
                "tier": tier,
                "record": f"{c['wins']}-{c['losses']}" + (f"-{c['pushes']}" if c["pushes"] else ""),
                "wins": c["wins"],
                "losses": c["losses"],
                "profit": round(c["profit"], 2),
                "profit_display": f"{'+' if c['profit'] >= 0 else ''}{c['profit']:.1f}u",
                "win_rate_pct": f"{wr:.1%}" if wr is not None else "N/A",
            })

        return BreakdownResponse(
            overall=overall,
            by_league=by_league,
            by_market=by_market,
            by_confidence=by_confidence,
        )
    finally:
        db.close()


def _classify_market(market: str) -> str:
    """Bucket a market name into a high-level category."""
    ml = market.lower()
    if "over" in ml or "under" in ml:
        return "Over/Under"
    if "win" in ml:
        return "Moneyline (Win)"
    if "draw" in ml:
        return "Moneyline (Draw)"
    if "-0.5" in ml or "+0.5" in ml:
        return "Spread"
    return "Other"


def _classify_confidence(units: float) -> str:
    """Map unit size to a confidence tier label."""
    if units >= 3.0:
        return "Very High (3u)"
    if units >= 2.0:
        return "High (2u)"
    if units >= 1.5:
        return "Moderate (1.5u)"
    return "Low (1u)"


@app.get("/picks/live", response_model=list[PickResponse])
def get_live_picks():
    """Currently pending (unresolved) picks."""
    db = _get_db()
    try:
        picks = db.get_pending_picks()
        return [_pick_to_response(p) for p in picks]
    finally:
        db.close()


@app.get("/picks/history", response_model=list[PickResponse])
def get_pick_history(
    limit: int = Query(50, ge=1, le=500, description="Max picks to return"),
    league_id: int | None = Query(None, description="Filter by league ID"),
    result: str | None = Query(None, description="Filter by result: win, loss, push"),
    days: int | None = Query(None, description="Only picks from the last N days"),
):
    """Resolved pick history with optional filters."""
    db = _get_db()
    try:
        all_picks = db.get_all_picks(limit=limit * 3, league_id=league_id)

        # Apply filters
        filtered = []
        cutoff = int(time.time()) - (days * 86400) if days else 0
        for p in all_picks:
            if result and p.result.value != result:
                continue
            if days and p.created_at < cutoff:
                continue
            filtered.append(p)
            if len(filtered) >= limit:
                break

        return [_pick_to_response(p) for p in filtered]
    finally:
        db.close()


@app.get("/player/{name}", response_model=PlayerResponse)
def get_player(
    name: str,
    league_id: int | None = Query(None, description="Filter to a specific league"),
):
    """Player stats, form, tier, and over rates."""
    db = _get_db()
    try:
        form = db.get_player_form(name, league_id=league_id)
        if form is None:
            # Try searching by handle pattern
            all_players = db.get_all_players(league_id=league_id)
            close = [p for p in all_players if name.lower() in extract_handle(p).lower()]
            if close:
                handle = extract_handle(close[0])
                form = db.get_player_form(handle, league_id=league_id)

        if form is None:
            raise HTTPException(status_code=404, detail=f"Player '{name}' not found")

        return PlayerResponse(
            handle=form.handle,
            league=league_display_name(form.league_id),
            league_id=form.league_id,
            tier=form.tier.label,
            form_trend=form.form_trend,
            form_modifier=round(form.form_modifier, 2),
            matches_played=form.matches_played,
            wins=form.wins,
            losses=form.losses,
            draws=form.draws,
            win_rate=round(form.win_rate, 4),
            win_rate_pct=f"{form.win_rate:.1%}",
            goals_scored=form.goals_scored,
            goals_conceded=form.goals_conceded,
            avg_goals_scored=round(form.avg_goals_scored, 2),
            avg_goals_conceded=round(form.avg_goals_conceded, 2),
            avg_total_goals=round(form.avg_total_goals, 2),
            over_rates={
                "2.5": round(form.over_2_5_rate, 4),
                "3.5": round(form.over_3_5_rate, 4),
                "4.5": round(form.over_4_5_rate, 4),
                "5.5": round(form.over_5_5_rate, 4),
            },
            recent={
                "matches": form.recent_matches,
                "wins": form.recent_wins,
                "losses": form.recent_losses,
                "draws": form.recent_draws,
                "win_rate": round(form.recent_win_rate, 4),
                "win_rate_pct": f"{form.recent_win_rate:.1%}",
                "goals_scored": form.recent_goals_scored,
                "goals_conceded": form.recent_goals_conceded,
                "avg_goals_scored": round(form.recent_avg_goals_scored, 2),
                "avg_goals_conceded": round(form.recent_avg_goals_conceded, 2),
            },
        )
    finally:
        db.close()


@app.get("/players", response_model=list[PlayerResponse])
def list_players(
    league_id: int | None = Query(None, description="Filter to a specific league"),
    min_matches: int = Query(10, ge=1, description="Minimum matches played"),
    tier: str | None = Query(None, description="Filter by tier: elite, solid, watchlist, new"),
):
    """List all players with form data."""
    db = _get_db()
    try:
        forms = db.get_all_player_forms(league_id=league_id, min_matches=min_matches)

        if tier:
            forms = [f for f in forms if f.tier.value == tier.lower()]

        results = []
        for form in forms:
            results.append(PlayerResponse(
                handle=form.handle,
                league=league_display_name(form.league_id),
                league_id=form.league_id,
                tier=form.tier.label,
                form_trend=form.form_trend,
                form_modifier=round(form.form_modifier, 2),
                matches_played=form.matches_played,
                wins=form.wins,
                losses=form.losses,
                draws=form.draws,
                win_rate=round(form.win_rate, 4),
                win_rate_pct=f"{form.win_rate:.1%}",
                goals_scored=form.goals_scored,
                goals_conceded=form.goals_conceded,
                avg_goals_scored=round(form.avg_goals_scored, 2),
                avg_goals_conceded=round(form.avg_goals_conceded, 2),
                avg_total_goals=round(form.avg_total_goals, 2),
                over_rates={
                    "2.5": round(form.over_2_5_rate, 4),
                    "3.5": round(form.over_3_5_rate, 4),
                    "4.5": round(form.over_4_5_rate, 4),
                    "5.5": round(form.over_5_5_rate, 4),
                },
                recent={
                    "matches": form.recent_matches,
                    "wins": form.recent_wins,
                    "losses": form.recent_losses,
                    "draws": form.recent_draws,
                    "win_rate": round(form.recent_win_rate, 4),
                    "win_rate_pct": f"{form.recent_win_rate:.1%}",
                    "goals_scored": form.recent_goals_scored,
                    "goals_conceded": form.recent_goals_conceded,
                    "avg_goals_scored": round(form.recent_avg_goals_scored, 2),
                    "avg_goals_conceded": round(form.recent_avg_goals_conceded, 2),
                },
            ))
        return results
    finally:
        db.close()


@app.get("/h2h/{player_a}/{player_b}", response_model=H2HResponse)
def get_h2h(
    player_a: str,
    player_b: str,
    limit: int = Query(20, ge=1, le=100, description="Max matches to return"),
):
    """Head-to-head history between two players."""
    db = _get_db()
    try:
        matches = db.get_h2h_matches(player_a, player_b, limit=limit)
        if not matches:
            raise HTTPException(
                status_code=404,
                detail=f"No H2H matches found between '{player_a}' and '{player_b}'",
            )

        # Build summary stats
        a_wins = 0
        b_wins = 0
        draws = 0
        total_goals = 0
        overs = {2.5: 0, 3.5: 0, 4.5: 0, 5.5: 0}

        for m in matches:
            total_goals += m.total_goals
            for line in overs:
                if m.total_goals > line:
                    overs[line] += 1
            if m.is_draw:
                draws += 1
            elif m.won_by(player_a):
                a_wins += 1
            else:
                b_wins += 1

        n = len(matches)
        return H2HResponse(
            player_a=player_a,
            player_b=player_b,
            total_matches=n,
            matches=[_match_to_response(m) for m in matches],
            summary={
                f"{player_a}_wins": a_wins,
                f"{player_b}_wins": b_wins,
                "draws": draws,
                "avg_total_goals": round(total_goals / n, 2) if n else 0,
                "over_rates": {
                    str(line): round(count / n, 4) if n else 0
                    for line, count in overs.items()
                },
            },
        )
    finally:
        db.close()


@app.get("/matches/recent", response_model=list[MatchResponse])
def get_recent_matches(
    limit: int = Query(50, ge=1, le=500, description="Max matches to return"),
    league_id: int | None = Query(None, description="Filter by league ID"),
    player: str | None = Query(None, description="Filter by player name"),
):
    """Recent completed match results."""
    db = _get_db()
    try:
        if player:
            matches = db.get_player_matches(player, limit=limit, league_id=league_id)
        else:
            # Get recent matches across all leagues
            conn = db._get_conn()
            if league_id:
                rows = conn.execute(
                    "SELECT * FROM matches WHERE league_id = ? ORDER BY start_time DESC LIMIT ?",
                    (league_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM matches ORDER BY start_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            matches = [db._row_to_match(r) for r in rows]

        return [_match_to_response(m) for m in matches]
    finally:
        db.close()


@app.get("/leagues", response_model=list[LeagueStatsResponse])
def get_leagues():
    """All tracked leagues with match counts and pick stats."""
    db = _get_db()
    try:
        results = []
        for league in League:
            lid = league.value
            s = db.get_pick_summary(league_id=lid)
            total = s["total"]
            win_rate = s["wins"] / total if total > 0 else None
            results.append(LeagueStatsResponse(
                league=league.display_name,
                league_id=lid,
                record=f"{s['wins']}-{s['losses']}" + (f"-{s['pushes']}" if s["pushes"] else ""),
                wins=s["wins"],
                losses=s["losses"],
                profit=round(s["profit"], 2),
                profit_display=f"{'+' if s['profit'] >= 0 else ''}{s['profit']:.1f}u",
                win_rate=win_rate,
                win_rate_pct=f"{win_rate:.1%}" if win_rate is not None else "N/A",
                total_matches=db.total_matches_for_league(lid),
            ))
        return results
    finally:
        db.close()


@app.get("/scan/status", response_model=ScanStatusResponse)
def get_scan_status():
    """Last scan info, total scans, and database health."""
    db = _get_db()
    try:
        total_scans = db.total_scans()
        history = db.get_scan_history(limit=1)

        last_scan = None
        if history:
            row = history[0]
            last_scan = {
                "scan_number": row["scan_number"],
                "started_at": row["started_at"],
                "started_at_fmt": _fmt_time(row["started_at"]),
                "finished_at": row["finished_at"],
                "finished_at_fmt": _fmt_time(row["finished_at"]),
                "duration_sec": row["finished_at"] - row["started_at"],
                "matches_found": row["matches_found"],
                "matches_with_odds": row["matches_with_odds"],
                "picks_generated": row["picks_generated"],
                "picks_alerted": row["picks_alerted"],
                "leagues_scanned": row["leagues_scanned"],
            }

        return ScanStatusResponse(
            total_scans=total_scans,
            last_scan=last_scan,
            database={
                "total_matches": db.total_matches(),
                "total_picks": db.get_pick_summary()["total"] + db.get_pick_summary()["pending"],
                "total_odds_snapshots": db.total_odds_snapshots(),
                "leagues": {
                    league.display_name: db.total_matches_for_league(league.value)
                    for league in League
                },
            },
        )
    finally:
        db.close()
