"""REST API for Esporf — exposes picks, stats, players, and match data.

Run with `esporf api` or directly: `uvicorn esporf.api:app`
Interactive docs available at /docs (Swagger UI).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from esporf.config import settings
from esporf.database import MatchDatabase
from esporf.models import (
    League,
    PickResult,
    PlayerForm,
    extract_handle,
    league_display_name,
)

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

app = FastAPI(
    title="Esporf API",
    description="eSoccer betting trend data — picks, stats, players, and match history.",
    version="1.0.0",
)

# CORS — allows the Vite dev server (port 5173) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


_API_PREFIXES = (
    "api", "docs", "redoc", "openapi.json",
)


def _setup_frontend():
    """Mount the built React frontend if the dist/ directory exists."""
    if _FRONTEND_DIR.is_dir():
        # Serve static assets (JS, CSS, images)
        app.mount(
            "/assets",
            StaticFiles(directory=str(_FRONTEND_DIR / "assets")),
            name="frontend-assets",
        )

        # Catch-all: serve index.html for any non-API route (SPA routing)
        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str):
            # Don't intercept API routes
            first_segment = path.split("/")[0]
            if first_segment in _API_PREFIXES:
                raise HTTPException(status_code=404)
            index = _FRONTEND_DIR / "index.html"
            if index.exists():
                return FileResponse(str(index))
            raise HTTPException(status_code=404)


# Deferred so API routes register first, SPA catch-all registers last
@app.on_event("startup")
def on_startup():
    _setup_frontend()
    # Rebuild player form if empty (e.g. after backfill without running scans)
    db = _get_db()
    try:
        forms = db.get_all_player_forms(min_matches=1)
        if not forms and db.total_matches() > 0:
            db.rebuild_player_form()
    finally:
        db.close()


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
    # Match-level stats (always available even without picks)
    total_matches: int = 0
    total_players: int = 0
    avg_total_goals: float | None = None
    avg_total_goals_display: str = "--"


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


class ScheduleMatchResponse(BaseModel):
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
    status: str = "completed"  # "completed", "upcoming", or "live"
    home_stats: dict | None = None
    away_stats: dict | None = None
    has_pick: bool = False


class ScheduleResponse(BaseModel):
    date: str
    matches: list[ScheduleMatchResponse]
    total: int


class MatchDetailResponse(BaseModel):
    match: MatchResponse
    home_form: PlayerResponse | None = None
    away_form: PlayerResponse | None = None
    h2h: dict | None = None
    picks: list[PickResponse] = []
    recommendation: dict | None = None


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


@app.get("/", include_in_schema=False)
def root():
    """Serve the frontend if available, otherwise redirect to API health."""
    index = _FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    # Fallback: if no frontend built, show the health check
    return _health_check()


@app.get("/api/", response_model=HealthResponse, include_in_schema=False)
@app.get("/api/health", response_model=HealthResponse)
def health():
    """API health check and basic info."""
    return _health_check()


def _health_check() -> HealthResponse:
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


@app.get("/api/stats", response_model=StatsResponse)
def get_stats(
    league_id: int | None = Query(None, description="Filter by league ID"),
    days: int | None = Query(None, description="Only include picks from the last N days"),
):
    """Overall record, units, win rate, and ROI."""
    db = _get_db()
    try:
        since = int(time.time()) - (days * 86400) if days else None
        summary = db.get_pick_summary(league_id=league_id, since=since)
        stats = _build_stats(summary)

        # Add match-level data
        conn = db._get_conn()
        if league_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, AVG(home_score + away_score) as avg_goals FROM matches WHERE league_id = ?",
                (league_id,),
            ).fetchone()
            players_row = conn.execute(
                """SELECT COUNT(DISTINCT handle) as cnt FROM player_form WHERE league_id = ?""",
                (league_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, AVG(home_score + away_score) as avg_goals FROM matches"
            ).fetchone()
            players_row = conn.execute(
                "SELECT COUNT(DISTINCT handle) as cnt FROM player_form"
            ).fetchone()

        stats.total_matches = row["cnt"]
        stats.total_players = players_row["cnt"]
        stats.avg_total_goals = round(row["avg_goals"], 1) if row["avg_goals"] else None
        stats.avg_total_goals_display = f"{row['avg_goals']:.1f}" if row["avg_goals"] else "--"
        return stats
    finally:
        db.close()


@app.get("/api/stats/breakdown", response_model=BreakdownResponse)
def get_stats_breakdown():
    """Full breakdown by league, by market, and by confidence tier."""
    db = _get_db()
    try:
        # Overall
        overall_summary = db.get_pick_summary()
        overall = _build_stats(overall_summary)

        # By league — use actual DB league IDs
        conn = db._get_conn()
        db_league_rows = conn.execute(
            "SELECT DISTINCT league_id FROM matches"
        ).fetchall()
        db_league_ids = {r["league_id"] for r in db_league_rows}
        all_league_ids = db_league_ids | {l.value for l in League}

        # Merge league IDs that share the same display name (e.g. legacy + current)
        name_to_ids: dict[str, list[int]] = {}
        for lid in sorted(all_league_ids):
            name = league_display_name(lid)
            name_to_ids.setdefault(name, []).append(lid)

        by_league = []
        for name, lids in name_to_ids.items():
            match_count = sum(db.total_matches_for_league(lid) for lid in lids)
            wins = losses = pushes = 0
            profit = 0.0
            for lid in lids:
                s = db.get_pick_summary(league_id=lid)
                wins += s["wins"]
                losses += s["losses"]
                pushes += s["pushes"]
                profit += s["profit"]
            total = wins + losses + pushes
            win_rate = wins / total if total > 0 else None
            by_league.append(LeagueStatsResponse(
                league=name,
                league_id=lids[0],
                record=f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
                wins=wins,
                losses=losses,
                profit=round(profit, 2),
                profit_display=f"{'+' if profit >= 0 else ''}{profit:.1f}u",
                win_rate=win_rate,
                win_rate_pct=f"{win_rate:.1%}" if win_rate is not None else "N/A",
                total_matches=match_count,
            ))
        by_league.sort(key=lambda x: x.total_matches, reverse=True)

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


@app.get("/api/picks/live", response_model=list[PickResponse])
def get_live_picks():
    """Currently pending (unresolved) picks."""
    db = _get_db()
    try:
        picks = db.get_pending_picks()
        return [_pick_to_response(p) for p in picks]
    finally:
        db.close()


@app.get("/api/picks/history", response_model=list[PickResponse])
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

        # Apply filters — always exclude pending (live) picks from history
        filtered = []
        cutoff = int(time.time()) - (days * 86400) if days else 0
        for p in all_picks:
            if p.result == PickResult.PENDING:
                continue
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


@app.get("/api/player/{name}", response_model=PlayerResponse)
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


@app.get("/api/players", response_model=list[PlayerResponse])
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


@app.get("/api/h2h/{player_a}/{player_b}", response_model=H2HResponse)
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


@app.get("/api/matches/recent", response_model=list[MatchResponse])
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


@app.get("/api/leagues", response_model=list[LeagueStatsResponse])
def get_leagues():
    """All tracked leagues with match counts and pick stats."""
    db = _get_db()
    try:
        # Get all league IDs actually present in the DB, plus configured ones
        conn = db._get_conn()
        db_league_rows = conn.execute(
            "SELECT DISTINCT league_id FROM matches"
        ).fetchall()
        db_league_ids = {r["league_id"] for r in db_league_rows}
        all_league_ids = db_league_ids | {l.value for l in League}

        # Merge league IDs that share the same display name (e.g. legacy + current)
        name_to_ids: dict[str, list[int]] = {}
        for lid in sorted(all_league_ids):
            name = league_display_name(lid)
            name_to_ids.setdefault(name, []).append(lid)

        results = []
        for name, lids in name_to_ids.items():
            match_count = sum(db.total_matches_for_league(lid) for lid in lids)
            wins = losses = pushes = 0
            profit = 0.0
            for lid in lids:
                s = db.get_pick_summary(league_id=lid)
                wins += s["wins"]
                losses += s["losses"]
                pushes += s["pushes"]
                profit += s["profit"]
            total = wins + losses + pushes
            win_rate = wins / total if total > 0 else None
            results.append(LeagueStatsResponse(
                league=name,
                league_id=lids[0],
                record=f"{wins}-{losses}" + (f"-{pushes}" if pushes else ""),
                wins=wins,
                losses=losses,
                profit=round(profit, 2),
                profit_display=f"{'+' if profit >= 0 else ''}{profit:.1f}u",
                win_rate=win_rate,
                win_rate_pct=f"{win_rate:.1%}" if win_rate is not None else "N/A",
                total_matches=match_count,
            ))
        # Sort by match count descending so most active leagues appear first
        results.sort(key=lambda x: x.total_matches, reverse=True)
        return results
    finally:
        db.close()


@app.get("/api/schedule", response_model=list[ScheduleResponse])
def get_schedule(
    days: int = Query(1, ge=1, le=7, description="Number of days to show (1-7)"),
    league_id: int | None = Query(None, description="Filter by league ID"),
):
    """Match schedule grouped by date with player stats for each match."""
    db = _get_db()
    try:
        conn = db._get_conn()
        cutoff = int(time.time()) - (days * 86400)

        # Fetch matches
        if league_id:
            rows = conn.execute(
                """SELECT * FROM matches WHERE start_time >= ? AND league_id = ?
                   ORDER BY start_time DESC""",
                (cutoff, league_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM matches WHERE start_time >= ?
                   ORDER BY start_time DESC""",
                (cutoff,),
            ).fetchall()

        matches = [db._row_to_match(r) for r in rows]

        # Get all pick match_ids for quick lookup
        pick_rows = conn.execute(
            "SELECT DISTINCT match_id FROM picks WHERE start_time >= ?",
            (cutoff,),
        ).fetchall()
        pick_match_ids = {r["match_id"] for r in pick_rows}

        # Cache player forms for efficiency
        form_cache: dict[str, PlayerForm | None] = {}

        def _get_form(handle: str, lid: int) -> PlayerForm | None:
            key = f"{handle.lower()}_{lid}"
            if key not in form_cache:
                form_cache[key] = db.get_player_form(handle, league_id=lid)
            return form_cache[key]

        # Group by date in the configured display timezone
        display_tz = ZoneInfo(settings.timezone)
        date_groups: dict[str, list[ScheduleMatchResponse]] = {}

        def _build_stats(handle: str, lid: int) -> dict | None:
            form = _get_form(handle, lid)
            if not form:
                return None
            return {
                "handle": form.handle,
                "win_rate_pct": f"{form.win_rate:.0%}",
                "avg_goals": round(form.avg_total_goals, 1),
                "over_2_5_pct": f"{form.over_2_5_rate:.0%}",
                "over_4_5_pct": f"{form.over_4_5_rate:.0%}",
                "form_trend": form.form_trend,
                "tier": form.tier.label,
                "matches": form.matches_played,
            }

        # Completed matches
        for m in matches:
            dt = datetime.fromtimestamp(m.start_time, tz=display_tz)
            date_key = dt.strftime("%Y-%m-%d")

            entry = ScheduleMatchResponse(
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
                status="completed",
                home_stats=_build_stats(extract_handle(m.home), m.league_id),
                away_stats=_build_stats(extract_handle(m.away), m.league_id),
                has_pick=m.match_id in pick_match_ids,
            )
            date_groups.setdefault(date_key, []).append(entry)

        # Upcoming / live matches from the latest scan
        completed_ids = {m.match_id for m in matches}
        upcoming_rows = db.get_upcoming(league_id)
        for row in upcoming_rows:
            if row["match_id"] in completed_ids:
                continue
            dt = datetime.fromtimestamp(row["start_time"], tz=display_tz)
            date_key = dt.strftime("%Y-%m-%d")
            is_live = bool(row["is_live"])

            entry = ScheduleMatchResponse(
                match_id=row["match_id"],
                league=league_display_name(row["league_id"]),
                league_id=row["league_id"],
                home=row["home"],
                away=row["away"],
                home_score=0,
                away_score=0,
                total_goals=0,
                score="vs",
                winner=None,
                start_time=row["start_time"],
                start_time_fmt=_fmt_time(row["start_time"]),
                status="live" if is_live else "upcoming",
                home_stats=_build_stats(extract_handle(row["home"]), row["league_id"]),
                away_stats=_build_stats(extract_handle(row["away"]), row["league_id"]),
                has_pick=row["match_id"] in pick_match_ids,
            )
            date_groups.setdefault(date_key, []).append(entry)

        result = []
        for date_key in sorted(date_groups.keys(), reverse=True):
            group = sorted(date_groups[date_key], key=lambda x: x.start_time, reverse=True)
            result.append(ScheduleResponse(
                date=date_key,
                matches=group,
                total=len(group),
            ))
        return result
    finally:
        db.close()


@app.get("/api/match/{match_id}/detail", response_model=MatchDetailResponse)
def get_match_detail(match_id: str):
    """Detailed match breakdown with player forms, H2H, picks, and recommendation."""
    db = _get_db()
    try:
        conn = db._get_conn()

        # 1. Get the match
        row = conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Match '{match_id}' not found")
        m = db._row_to_match(row)
        match_resp = _match_to_response(m)

        # 2. Player forms
        home_handle = extract_handle(m.home)
        away_handle = extract_handle(m.away)
        home_form_data = db.get_player_form(home_handle, league_id=m.league_id)
        away_form_data = db.get_player_form(away_handle, league_id=m.league_id)

        home_resp = _form_to_response(home_form_data) if home_form_data else None
        away_resp = _form_to_response(away_form_data) if away_form_data else None

        # 3. H2H
        h2h_matches = db.get_h2h_matches(m.home, m.away, limit=20)
        h2h_data = None
        if h2h_matches:
            a_wins = sum(1 for x in h2h_matches if x.won_by(m.home))
            b_wins = sum(1 for x in h2h_matches if x.won_by(m.away))
            draws = sum(1 for x in h2h_matches if x.is_draw)
            total_goals = sum(x.total_goals for x in h2h_matches)
            n = len(h2h_matches)
            overs = {2.5: 0, 3.5: 0, 4.5: 0, 5.5: 0}
            for x in h2h_matches:
                for line in overs:
                    if x.total_goals > line:
                        overs[line] += 1
            h2h_data = {
                "total_matches": n,
                "home_wins": a_wins,
                "away_wins": b_wins,
                "draws": draws,
                "avg_total_goals": round(total_goals / n, 2) if n else 0,
                "over_rates": {
                    str(line): round(count / n, 2) if n else 0
                    for line, count in overs.items()
                },
                "recent": [
                    {
                        "home": x.home,
                        "away": x.away,
                        "score": x.score_str(),
                        "total_goals": x.total_goals,
                        "start_time_fmt": _fmt_time(x.start_time),
                    }
                    for x in h2h_matches[:10]
                ],
            }

        # 4. Picks on this match
        pick_rows = conn.execute(
            "SELECT * FROM picks WHERE match_id = ?", (match_id,)
        ).fetchall()
        picks = [_pick_to_response(db._row_to_pick(r)) for r in pick_rows]

        # 5. Recommendation — build from available trend data
        recommendation = None
        if home_form_data and away_form_data:
            # Compare stats to generate a recommendation
            rec_lines = []
            confidence_factors = []

            # Over/Under analysis from combined player data
            home_avg_goals = home_form_data.avg_total_goals
            away_avg_goals = away_form_data.avg_total_goals
            expected_goals = (home_avg_goals + away_avg_goals) / 2

            # H2H avg goals factor
            if h2h_data:
                h2h_avg = h2h_data["avg_total_goals"]
                expected_goals = (expected_goals + h2h_avg * 2) / 3  # H2H weighted double

            # Find the best over/under line
            for line in [2.5, 3.5, 4.5, 5.5]:
                home_rate = getattr(home_form_data, f"over_{str(line).replace('.', '_')}_rate", 0)
                away_rate = getattr(away_form_data, f"over_{str(line).replace('.', '_')}_rate", 0)
                combined_rate = (home_rate + away_rate) / 2

                h2h_rate = None
                if h2h_data and str(line) in h2h_data["over_rates"]:
                    h2h_rate = h2h_data["over_rates"][str(line)]
                    combined_rate = (combined_rate + h2h_rate * 2) / 3

                if combined_rate >= 0.70:
                    rec_lines.append({
                        "market": f"Over {line} Goals",
                        "combined_rate": round(combined_rate, 2),
                        "combined_rate_pct": f"{combined_rate:.0%}",
                        "home_rate": round(home_rate, 2),
                        "away_rate": round(away_rate, 2),
                        "h2h_rate": round(h2h_rate, 2) if h2h_rate is not None else None,
                    })

                under_rate = 1.0 - combined_rate
                if under_rate >= 0.70:
                    rec_lines.append({
                        "market": f"Under {line} Goals",
                        "combined_rate": round(under_rate, 2),
                        "combined_rate_pct": f"{under_rate:.0%}",
                        "home_rate": round(1.0 - home_rate, 2),
                        "away_rate": round(1.0 - away_rate, 2),
                        "h2h_rate": round(1.0 - h2h_rate, 2) if h2h_rate is not None else None,
                    })

            # Sort by combined rate descending
            rec_lines.sort(key=lambda x: x["combined_rate"], reverse=True)

            # Moneyline recommendation
            ml_rec = None
            if h2h_data and h2h_data["total_matches"] >= 5:
                total_h2h = h2h_data["total_matches"]
                hw = h2h_data["home_wins"]
                aw = h2h_data["away_wins"]
                dr = h2h_data["draws"]

                home_wr = home_form_data.recent_win_rate
                away_wr = away_form_data.recent_win_rate

                if hw / total_h2h >= 0.60 and home_wr >= 0.40:
                    ml_rec = {
                        "side": extract_handle(m.home),
                        "h2h_rate_pct": f"{hw / total_h2h:.0%}",
                        "recent_form_pct": f"{home_wr:.0%}",
                    }
                elif aw / total_h2h >= 0.60 and away_wr >= 0.40:
                    ml_rec = {
                        "side": extract_handle(m.away),
                        "h2h_rate_pct": f"{aw / total_h2h:.0%}",
                        "recent_form_pct": f"{away_wr:.0%}",
                    }

            recommendation = {
                "expected_goals": round(expected_goals, 1),
                "best_lines": rec_lines[:3],
                "moneyline": ml_rec,
                "home_tier": home_form_data.tier.label,
                "away_tier": away_form_data.tier.label,
                "home_trend": home_form_data.form_trend,
                "away_trend": away_form_data.form_trend,
            }

        return MatchDetailResponse(
            match=match_resp,
            home_form=home_resp,
            away_form=away_resp,
            h2h=h2h_data,
            picks=picks,
            recommendation=recommendation,
        )
    finally:
        db.close()


def _form_to_response(form: PlayerForm) -> PlayerResponse:
    """Convert a PlayerForm to a PlayerResponse."""
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


@app.get("/api/scan/status", response_model=ScanStatusResponse)
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
