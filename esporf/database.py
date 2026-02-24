"""SQLite database for storing eSoccer match history.

All completed matches are stored here so the trend analyzer can query
player histories, head-to-head records, and goal patterns without
needing to re-fetch from the API each time.
"""

from __future__ import annotations

import logging
import os
import sqlite3

import time

from esporf.config import settings
from esporf.models import MatchResult, PickResult, PlayerForm, TrackedPick, extract_handle

logger = logging.getLogger(__name__)

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    league_id INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    home_score INTEGER NOT NULL,
    away_score INTEGER NOT NULL,
    start_time INTEGER NOT NULL,
    ht_home_score INTEGER,
    ht_away_score INTEGER
);
"""

CREATE_PICKS_TABLE = """
CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    league_id INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    market TEXT NOT NULL,
    units REAL NOT NULL,
    odds REAL,
    hit_rate REAL NOT NULL,
    edge REAL,
    result TEXT NOT NULL DEFAULT 'pending',
    profit REAL NOT NULL DEFAULT 0.0,
    home_score INTEGER,
    away_score INTEGER,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER,
    UNIQUE(match_id, market)
);
"""

CREATE_PLAYER_FORM_TABLE = """
CREATE TABLE IF NOT EXISTS player_form (
    handle TEXT NOT NULL,
    league_id INTEGER NOT NULL,
    matches_played INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0,
    goals_scored INTEGER NOT NULL DEFAULT 0,
    goals_conceded INTEGER NOT NULL DEFAULT 0,
    avg_goals_scored REAL NOT NULL DEFAULT 0.0,
    avg_goals_conceded REAL NOT NULL DEFAULT 0.0,
    win_rate REAL NOT NULL DEFAULT 0.0,
    over_2_5_rate REAL NOT NULL DEFAULT 0.0,
    over_3_5_rate REAL NOT NULL DEFAULT 0.0,
    over_4_5_rate REAL NOT NULL DEFAULT 0.0,
    over_5_5_rate REAL NOT NULL DEFAULT 0.0,
    recent_matches INTEGER NOT NULL DEFAULT 0,
    recent_wins INTEGER NOT NULL DEFAULT 0,
    recent_losses INTEGER NOT NULL DEFAULT 0,
    recent_draws INTEGER NOT NULL DEFAULT 0,
    recent_goals_scored INTEGER NOT NULL DEFAULT 0,
    recent_goals_conceded INTEGER NOT NULL DEFAULT 0,
    recent_win_rate REAL NOT NULL DEFAULT 0.0,
    recent_over_2_5_rate REAL NOT NULL DEFAULT 0.0,
    last_updated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (handle, league_id)
);
"""

CREATE_ODDS_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    league_id INTEGER NOT NULL,
    home TEXT NOT NULL,
    away TEXT NOT NULL,
    start_time INTEGER NOT NULL,
    line REAL,
    over_odds REAL,
    under_odds REAL,
    home_ml REAL,
    draw_ml REAL,
    away_ml REAL,
    spread REAL,
    spread_home_odds REAL,
    spread_away_odds REAL,
    source TEXT NOT NULL DEFAULT 'bet365',
    captured_at INTEGER NOT NULL
);
"""

CREATE_SCAN_LOG_TABLE = """
CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_number INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL,
    matches_found INTEGER NOT NULL DEFAULT 0,
    matches_with_odds INTEGER NOT NULL DEFAULT 0,
    picks_generated INTEGER NOT NULL DEFAULT 0,
    picks_alerted INTEGER NOT NULL DEFAULT 0,
    leagues_scanned TEXT NOT NULL DEFAULT ''
);
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_matches_home ON matches(home);",
    "CREATE INDEX IF NOT EXISTS idx_matches_away ON matches(away);",
    "CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league_id);",
    "CREATE INDEX IF NOT EXISTS idx_matches_time ON matches(start_time DESC);",
    "CREATE INDEX IF NOT EXISTS idx_matches_home_away ON matches(home, away);",
    "CREATE INDEX IF NOT EXISTS idx_picks_result ON picks(result);",
    "CREATE INDEX IF NOT EXISTS idx_picks_time ON picks(created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_picks_league ON picks(league_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_picks_match_market ON picks(match_id, market);",
    "CREATE INDEX IF NOT EXISTS idx_odds_match ON odds_snapshots(match_id);",
    "CREATE INDEX IF NOT EXISTS idx_odds_time ON odds_snapshots(captured_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_odds_league ON odds_snapshots(league_id);",
    "CREATE INDEX IF NOT EXISTS idx_odds_match_line ON odds_snapshots(match_id, line);",
    "CREATE INDEX IF NOT EXISTS idx_scan_log_time ON scan_log(started_at DESC);",
]


class MatchDatabase:
    """SQLite-backed match history store."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or settings.db_path
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(CREATE_TABLE)
        conn.execute(CREATE_PICKS_TABLE)
        conn.execute(CREATE_PLAYER_FORM_TABLE)
        conn.execute(CREATE_ODDS_SNAPSHOTS_TABLE)
        conn.execute(CREATE_SCAN_LOG_TABLE)
        self._deduplicate_picks(conn)
        for idx_sql in CREATE_INDEXES:
            conn.execute(idx_sql)
        conn.commit()

    def _deduplicate_picks(self, conn: sqlite3.Connection) -> None:
        """Remove duplicate picks, keeping the oldest per logical match.

        Duplicates happen when the bot restarts and re-records picks for
        the same match.  The match_id can differ between runs (e.g. kambi_
        vs BetsAPI numeric ID), so we group by player handles + approximate
        start time + market instead.
        """
        rows = conn.execute(
            "SELECT id, home, away, start_time, market FROM picks ORDER BY id ASC"
        ).fetchall()
        if not rows:
            return

        seen: dict[str, int] = {}  # dedup_key → first row id
        to_delete: list[int] = []

        for r in rows:
            h = extract_handle(r["home"]).lower()
            a = extract_handle(r["away"]).lower()
            pair = tuple(sorted([h, a]))
            rounded_time = round(r["start_time"] / 600) * 600
            key = f"{pair[0]}_{pair[1]}_{rounded_time}_{r['market']}"

            if key in seen:
                to_delete.append(r["id"])
            else:
                seen[key] = r["id"]

        if not to_delete:
            return

        # Delete in batches
        for i in range(0, len(to_delete), 100):
            batch = to_delete[i:i + 100]
            placeholders = ",".join("?" * len(batch))
            conn.execute(f"DELETE FROM picks WHERE id IN ({placeholders})", batch)
        conn.commit()
        logger.info("Deduplicated picks: removed %d duplicate row(s)", len(to_delete))

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Writes ───────────────────────────────────────────────────────

    def insert_match(self, match: MatchResult) -> bool:
        """Insert a match. Returns True if new, False if duplicate."""
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO matches
                   (match_id, league_id, home, away, home_score, away_score,
                    start_time, ht_home_score, ht_away_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    match.match_id, match.league_id, match.home, match.away,
                    match.home_score, match.away_score, match.start_time,
                    match.ht_home_score, match.ht_away_score,
                ),
            )
            conn.commit()
            return conn.total_changes > 0
        except sqlite3.IntegrityError:
            return False

    def insert_many(self, matches: list[MatchResult]) -> int:
        """Insert multiple matches. Returns count of new matches added."""
        conn = self._get_conn()
        before = conn.total_changes
        for match in matches:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO matches
                       (match_id, league_id, home, away, home_score, away_score,
                        start_time, ht_home_score, ht_away_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        match.match_id, match.league_id, match.home, match.away,
                        match.home_score, match.away_score, match.start_time,
                        match.ht_home_score, match.ht_away_score,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
        conn.commit()
        return conn.total_changes - before

    # ── Reads ────────────────────────────────────────────────────────

    @staticmethod
    def _handle_pattern(player: str) -> str:
        """Build a LIKE pattern that matches any team name for a player handle.

        'Bayer 04 (Sheva)' → '%(Sheva)' so it matches 'Arsenal (Sheva)' too.
        Plain names without parentheses (e.g. 'Alpha') match exactly.
        """
        handle = extract_handle(player)
        # If the handle is the same as the input, the name has no parentheses
        # — match it exactly rather than as a broken LIKE pattern.
        if handle == player:
            return player
        return f"%({handle})"

    def get_player_matches(
        self, player: str, limit: int = 50, league_id: int | None = None
    ) -> list[MatchResult]:
        """Get a player's most recent matches (home or away).

        Matches by handle so 'Bayer 04 (Sheva)' finds matches where
        the same player used any team name.
        """
        conn = self._get_conn()
        pattern = self._handle_pattern(player)
        if league_id:
            rows = conn.execute(
                """SELECT * FROM matches
                   WHERE (home LIKE ? OR away LIKE ?) AND league_id = ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, pattern, league_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM matches
                   WHERE home LIKE ? OR away LIKE ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, pattern, limit),
            ).fetchall()
        return [self._row_to_match(r) for r in rows]

    def get_player_home_matches(
        self, player: str, limit: int = 50, league_id: int | None = None
    ) -> list[MatchResult]:
        """Get matches where the player was the home side."""
        conn = self._get_conn()
        pattern = self._handle_pattern(player)
        if league_id:
            rows = conn.execute(
                """SELECT * FROM matches
                   WHERE home LIKE ? AND league_id = ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, league_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM matches WHERE home LIKE ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, limit),
            ).fetchall()
        return [self._row_to_match(r) for r in rows]

    def get_player_away_matches(
        self, player: str, limit: int = 50, league_id: int | None = None
    ) -> list[MatchResult]:
        """Get matches where the player was the away side."""
        conn = self._get_conn()
        pattern = self._handle_pattern(player)
        if league_id:
            rows = conn.execute(
                """SELECT * FROM matches
                   WHERE away LIKE ? AND league_id = ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, league_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM matches WHERE away LIKE ?
                   ORDER BY start_time DESC LIMIT ?""",
                (pattern, limit),
            ).fetchall()
        return [self._row_to_match(r) for r in rows]

    def get_h2h_matches(
        self, player_a: str, player_b: str, limit: int = 50
    ) -> list[MatchResult]:
        """Get head-to-head matches between two players (either side)."""
        conn = self._get_conn()
        pa = self._handle_pattern(player_a)
        pb = self._handle_pattern(player_b)
        rows = conn.execute(
            """SELECT * FROM matches
               WHERE (home LIKE ? AND away LIKE ?) OR (home LIKE ? AND away LIKE ?)
               ORDER BY start_time DESC LIMIT ?""",
            (pa, pb, pb, pa, limit),
        ).fetchall()
        return [self._row_to_match(r) for r in rows]

    def get_all_players(self, league_id: int | None = None) -> list[str]:
        """Get a list of all unique players in the database."""
        conn = self._get_conn()
        if league_id:
            rows = conn.execute(
                """SELECT DISTINCT home AS player FROM matches WHERE league_id = ?
                   UNION
                   SELECT DISTINCT away AS player FROM matches WHERE league_id = ?""",
                (league_id, league_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT DISTINCT home AS player FROM matches
                   UNION
                   SELECT DISTINCT away AS player FROM matches""",
            ).fetchall()
        return [r["player"] for r in rows]

    def total_matches(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM matches").fetchone()
        return row["cnt"]

    def total_matches_for_league(self, league_id: int) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM matches WHERE league_id = ?", (league_id,)
        ).fetchone()
        return row["cnt"]

    # ── Pick tracking ────────────────────────────────────────────────

    def insert_pick(self, pick: TrackedPick) -> int | None:
        """Insert a tracked pick. Returns the new row ID, or None if duplicate."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT OR IGNORE INTO picks
               (match_id, league_id, home, away, start_time, market, units,
                odds, hit_rate, edge, result, profit, home_score, away_score,
                created_at, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pick.match_id, pick.league_id, pick.home, pick.away,
                pick.start_time, pick.market, pick.units,
                pick.odds, pick.hit_rate, pick.edge,
                pick.result.value, pick.profit, pick.home_score,
                pick.away_score, pick.created_at, pick.resolved_at,
            ),
        )
        conn.commit()
        if cur.lastrowid and cur.rowcount > 0:
            return cur.lastrowid
        return None

    def get_recent_pick_matches(self, since: int) -> list[tuple[str, str]]:
        """Get (home, away) pairs for picks created since a timestamp.

        Used to populate the in-memory alerted set on startup so we don't
        re-alert matches that were already picked before a restart.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT home, away, start_time FROM picks WHERE created_at >= ?",
            (since,),
        ).fetchall()
        return [(r["home"], r["away"], r["start_time"]) for r in rows]

    def get_pending_picks(self) -> list[TrackedPick]:
        """Get all picks awaiting resolution."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM picks WHERE result = 'pending' ORDER BY start_time ASC"
        ).fetchall()
        return [self._row_to_pick(r) for r in rows]

    def resolve_pick(
        self, pick_id: int, result: PickResult, profit: float,
        home_score: int, away_score: int, resolved_at: int,
    ) -> None:
        """Update a pick with its outcome."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE picks
               SET result = ?, profit = ?, home_score = ?, away_score = ?,
                   resolved_at = ?
               WHERE id = ?""",
            (result.value, profit, home_score, away_score, resolved_at, pick_id),
        )
        conn.commit()

    def get_all_picks(
        self, limit: int = 100, league_id: int | None = None
    ) -> list[TrackedPick]:
        """Get picks ordered by most recent first."""
        conn = self._get_conn()
        if league_id:
            rows = conn.execute(
                """SELECT * FROM picks WHERE league_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (league_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM picks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_pick(r) for r in rows]

    def get_pick_summary(
        self,
        league_id: int | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> dict[str, int | float]:
        """Get aggregate W/L/P stats and profit.

        Args:
            league_id: Filter to a specific league.
            since: Only include picks created at or after this unix timestamp.
            until: Only include picks created before this unix timestamp.

        Returns dict with keys: wins, losses, pushes, pending,
        total, profit, units_wagered.
        """
        conn = self._get_conn()
        clauses: list[str] = []
        params: list[int | float] = []
        if league_id:
            clauses.append("league_id = ?")
            params.append(league_id)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at < ?")
            params.append(until)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT result, profit, units FROM picks{where}", params
        ).fetchall()

        wins = losses = pushes = pending = 0
        total_profit = 0.0
        units_wagered = 0.0

        for r in rows:
            units_wagered += r["units"]
            total_profit += r["profit"]
            match r["result"]:
                case "win":
                    wins += 1
                case "loss":
                    losses += 1
                case "push":
                    pushes += 1
                case "pending":
                    pending += 1

        return {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "total": wins + losses + pushes,
            "profit": total_profit,
            "units_wagered": units_wagered,
        }

    # ── Player form tracking ─────────────────────────────────────────

    def rebuild_player_form(self, league_ids: list[int] | None = None) -> int:
        """Recompute player_form for every player from the matches table.

        Args:
            league_ids: Leagues to rebuild.  Defaults to all leagues in DB.

        Returns the number of player rows written.
        """
        conn = self._get_conn()
        now = int(time.time())

        if league_ids is None:
            rows = conn.execute(
                "SELECT DISTINCT league_id FROM matches"
            ).fetchall()
            league_ids = [r["league_id"] for r in rows]

        written = 0
        for lid in league_ids:
            players = self.get_all_players(league_id=lid)
            for player_name in players:
                handle = extract_handle(player_name)
                pattern = self._handle_pattern(player_name)

                # All matches for this player in this league
                all_rows = conn.execute(
                    """SELECT home, away, home_score, away_score
                       FROM matches
                       WHERE (home LIKE ? OR away LIKE ?) AND league_id = ?
                       ORDER BY start_time DESC""",
                    (pattern, pattern, lid),
                ).fetchall()

                if not all_rows:
                    continue

                form = self._compute_form_from_rows(handle, lid, all_rows, now)
                self._upsert_player_form(conn, form)
                written += 1

        conn.commit()
        logger.info("Rebuilt player_form: %d rows across %d league(s)", written, len(league_ids))
        return written

    @staticmethod
    def _compute_form_from_rows(
        handle: str, league_id: int, rows: list[sqlite3.Row], now: int,
    ) -> PlayerForm:
        """Compute a PlayerForm from raw match rows (newest first)."""
        total = len(rows)
        wins = losses = draws = gs = gc = 0
        over_2_5 = over_3_5 = over_4_5 = over_5_5 = 0

        # Recent = last 10
        recent_n = min(10, total)
        r_wins = r_losses = r_draws = r_gs = r_gc = r_o25 = 0

        for i, r in enumerate(rows):
            h_handle = extract_handle(r["home"]).lower()
            is_home = handle.lower() in h_handle or h_handle in handle.lower()

            if is_home:
                gf = r["home_score"]
                ga = r["away_score"]
            else:
                gf = r["away_score"]
                ga = r["home_score"]

            gs += gf
            gc += ga
            total_goals = gf + ga

            if gf > ga:
                wins += 1
            elif gf < ga:
                losses += 1
            else:
                draws += 1

            if total_goals > 2.5:
                over_2_5 += 1
            if total_goals > 3.5:
                over_3_5 += 1
            if total_goals > 4.5:
                over_4_5 += 1
            if total_goals > 5.5:
                over_5_5 += 1

            # Recent form window
            if i < recent_n:
                r_gs += gf
                r_gc += ga
                if total_goals > 2.5:
                    r_o25 += 1
                if gf > ga:
                    r_wins += 1
                elif gf < ga:
                    r_losses += 1
                else:
                    r_draws += 1

        return PlayerForm(
            handle=handle,
            league_id=league_id,
            matches_played=total,
            wins=wins,
            losses=losses,
            draws=draws,
            goals_scored=gs,
            goals_conceded=gc,
            avg_goals_scored=gs / total if total else 0.0,
            avg_goals_conceded=gc / total if total else 0.0,
            win_rate=wins / total if total else 0.0,
            over_2_5_rate=over_2_5 / total if total else 0.0,
            over_3_5_rate=over_3_5 / total if total else 0.0,
            over_4_5_rate=over_4_5 / total if total else 0.0,
            over_5_5_rate=over_5_5 / total if total else 0.0,
            recent_matches=recent_n,
            recent_wins=r_wins,
            recent_losses=r_losses,
            recent_draws=r_draws,
            recent_goals_scored=r_gs,
            recent_goals_conceded=r_gc,
            recent_win_rate=r_wins / recent_n if recent_n else 0.0,
            recent_over_2_5_rate=r_o25 / recent_n if recent_n else 0.0,
            last_updated=now,
        )

    @staticmethod
    def _upsert_player_form(conn: sqlite3.Connection, f: PlayerForm) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO player_form
               (handle, league_id, matches_played, wins, losses, draws,
                goals_scored, goals_conceded, avg_goals_scored, avg_goals_conceded,
                win_rate, over_2_5_rate, over_3_5_rate, over_4_5_rate, over_5_5_rate,
                recent_matches, recent_wins, recent_losses, recent_draws,
                recent_goals_scored, recent_goals_conceded, recent_win_rate,
                recent_over_2_5_rate, last_updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f.handle, f.league_id, f.matches_played, f.wins, f.losses, f.draws,
                f.goals_scored, f.goals_conceded, f.avg_goals_scored, f.avg_goals_conceded,
                f.win_rate, f.over_2_5_rate, f.over_3_5_rate, f.over_4_5_rate, f.over_5_5_rate,
                f.recent_matches, f.recent_wins, f.recent_losses, f.recent_draws,
                f.recent_goals_scored, f.recent_goals_conceded, f.recent_win_rate,
                f.recent_over_2_5_rate, f.last_updated,
            ),
        )

    def get_player_form(
        self, handle: str, league_id: int | None = None,
    ) -> PlayerForm | None:
        """Look up a single player's form stats."""
        conn = self._get_conn()
        h = handle.lower()
        if league_id:
            row = conn.execute(
                "SELECT * FROM player_form WHERE LOWER(handle) = ? AND league_id = ?",
                (h, league_id),
            ).fetchone()
        else:
            # Return the row with the most matches if multiple leagues
            row = conn.execute(
                "SELECT * FROM player_form WHERE LOWER(handle) = ? ORDER BY matches_played DESC LIMIT 1",
                (h,),
            ).fetchone()
        return self._row_to_player_form(row) if row else None

    def get_all_player_forms(
        self, league_id: int | None = None, min_matches: int = 0,
    ) -> list[PlayerForm]:
        """Get all player form entries, optionally filtered."""
        conn = self._get_conn()
        clauses = ["matches_played >= ?"]
        params: list[int] = [min_matches]
        if league_id:
            clauses.append("league_id = ?")
            params.append(league_id)
        where = " WHERE " + " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT * FROM player_form{where} ORDER BY win_rate DESC",
            params,
        ).fetchall()
        return [self._row_to_player_form(r) for r in rows]

    @staticmethod
    def _row_to_player_form(row: sqlite3.Row) -> PlayerForm:
        return PlayerForm(
            handle=row["handle"],
            league_id=row["league_id"],
            matches_played=row["matches_played"],
            wins=row["wins"],
            losses=row["losses"],
            draws=row["draws"],
            goals_scored=row["goals_scored"],
            goals_conceded=row["goals_conceded"],
            avg_goals_scored=row["avg_goals_scored"],
            avg_goals_conceded=row["avg_goals_conceded"],
            win_rate=row["win_rate"],
            over_2_5_rate=row["over_2_5_rate"],
            over_3_5_rate=row["over_3_5_rate"],
            over_4_5_rate=row["over_4_5_rate"],
            over_5_5_rate=row["over_5_5_rate"],
            recent_matches=row["recent_matches"],
            recent_wins=row["recent_wins"],
            recent_losses=row["recent_losses"],
            recent_draws=row["recent_draws"],
            recent_goals_scored=row["recent_goals_scored"],
            recent_goals_conceded=row["recent_goals_conceded"],
            recent_win_rate=row["recent_win_rate"],
            recent_over_2_5_rate=row["recent_over_2_5_rate"],
            last_updated=row["last_updated"],
        )

    @staticmethod
    def _row_to_pick(row: sqlite3.Row) -> TrackedPick:
        return TrackedPick(
            id=row["id"],
            match_id=row["match_id"],
            league_id=row["league_id"],
            home=row["home"],
            away=row["away"],
            start_time=row["start_time"],
            market=row["market"],
            units=row["units"],
            odds=row["odds"],
            hit_rate=row["hit_rate"],
            edge=row["edge"],
            result=PickResult(row["result"]),
            profit=row["profit"],
            home_score=row["home_score"],
            away_score=row["away_score"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    # ── Odds snapshots ────────────────────────────────────────────────

    def snapshot_odds(self, matches: list, now: int | None = None) -> int:
        """Store a snapshot of current odds for a list of UpcomingMatch objects.

        Captures every O/U line, moneyline, and spread available on each
        match.  Called once per scan cycle so line movements accumulate
        over time.

        Returns the number of rows written.
        """
        if now is None:
            now = int(time.time())
        conn = self._get_conn()
        written = 0

        for m in matches:
            odds = m.odds
            if odds is None or not odds.has_data:
                continue

            # O/U total lines
            for ol in odds.total_lines:
                conn.execute(
                    """INSERT INTO odds_snapshots
                       (match_id, league_id, home, away, start_time,
                        line, over_odds, under_odds, source, captured_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (m.match_id, m.league_id, m.home, m.away,
                     m.start_time, ol.line, ol.over_odds, ol.under_odds,
                     ol.source, now),
                )
                written += 1

            # Moneyline
            if odds.moneyline:
                ml = odds.moneyline
                conn.execute(
                    """INSERT INTO odds_snapshots
                       (match_id, league_id, home, away, start_time,
                        home_ml, draw_ml, away_ml, source, captured_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (m.match_id, m.league_id, m.home, m.away,
                     m.start_time, ml.home_odds, ml.draw_odds,
                     ml.away_odds, ml.source, now),
                )
                written += 1

            # Spreads
            for sl in odds.spreads:
                conn.execute(
                    """INSERT INTO odds_snapshots
                       (match_id, league_id, home, away, start_time,
                        spread, spread_home_odds, spread_away_odds,
                        source, captured_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (m.match_id, m.league_id, m.home, m.away,
                     m.start_time, sl.handicap, sl.home_odds,
                     sl.away_odds, sl.source, now),
                )
                written += 1

        conn.commit()
        return written

    def get_odds_history(
        self, match_id: str, line: float | None = None,
    ) -> list[sqlite3.Row]:
        """Get odds snapshot history for a match, optionally filtered by line."""
        conn = self._get_conn()
        if line is not None:
            return conn.execute(
                """SELECT * FROM odds_snapshots
                   WHERE match_id = ? AND line = ?
                   ORDER BY captured_at ASC""",
                (match_id, line),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM odds_snapshots WHERE match_id = ? ORDER BY captured_at ASC",
            (match_id,),
        ).fetchall()

    def get_recent_odds(self, limit: int = 100) -> list[sqlite3.Row]:
        """Get the most recent odds snapshots across all matches."""
        conn = self._get_conn()
        return conn.execute(
            "SELECT * FROM odds_snapshots ORDER BY captured_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def total_odds_snapshots(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM odds_snapshots").fetchone()
        return row["cnt"]

    # ── Scan log ──────────────────────────────────────────────────────

    def log_scan(
        self,
        scan_number: int,
        started_at: int,
        finished_at: int,
        matches_found: int = 0,
        matches_with_odds: int = 0,
        picks_generated: int = 0,
        picks_alerted: int = 0,
        leagues_scanned: str = "",
    ) -> int:
        """Record metadata about a completed scan cycle. Returns row ID."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO scan_log
               (scan_number, started_at, finished_at, matches_found,
                matches_with_odds, picks_generated, picks_alerted,
                leagues_scanned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (scan_number, started_at, finished_at, matches_found,
             matches_with_odds, picks_generated, picks_alerted,
             leagues_scanned),
        )
        conn.commit()
        return cur.lastrowid

    def get_scan_history(self, limit: int = 50) -> list[sqlite3.Row]:
        """Get recent scan log entries."""
        conn = self._get_conn()
        return conn.execute(
            "SELECT * FROM scan_log ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def total_scans(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM scan_log").fetchone()
        return row["cnt"]

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_match(row: sqlite3.Row) -> MatchResult:
        return MatchResult(
            match_id=row["match_id"],
            league_id=row["league_id"],
            home=row["home"],
            away=row["away"],
            home_score=row["home_score"],
            away_score=row["away_score"],
            start_time=row["start_time"],
            ht_home_score=row["ht_home_score"],
            ht_away_score=row["ht_away_score"],
        )
