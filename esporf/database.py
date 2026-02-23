"""SQLite database for storing eSoccer match history.

All completed matches are stored here so the trend analyzer can query
player histories, head-to-head records, and goal patterns without
needing to re-fetch from the API each time.
"""

from __future__ import annotations

import logging
import os
import sqlite3

from esporf.config import settings
from esporf.models import MatchResult, PickResult, TrackedPick, extract_handle

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
    resolved_at INTEGER
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
        for idx_sql in CREATE_INDEXES:
            conn.execute(idx_sql)
        conn.commit()

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

    def insert_pick(self, pick: TrackedPick) -> int:
        """Insert a tracked pick. Returns the new row ID."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO picks
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
        return cur.lastrowid

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
