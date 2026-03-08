"""Match context analysis — situational factors beyond raw trend statistics.

Computes contextual modifiers that adjust pick confidence based on:
1. Time-of-day performance patterns
2. Player fatigue (recent match volume)
3. Short-term streak momentum
4. Goal margin consistency (blowout detection)

Each factor returns a modifier (0.0-1.5 range) that scales the final
confidence score. Neutral = 1.0, penalty < 1.0, bonus > 1.0.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from esporf.database import MatchDatabase
from esporf.models import extract_handle

logger = logging.getLogger(__name__)

# Time-of-day buckets: 4-hour windows (UTC)
_TOD_BUCKETS = {
    0: "00-04",
    1: "04-08",
    2: "08-12",
    3: "12-16",
    4: "16-20",
    5: "20-24",
}

# Fatigue thresholds
_FATIGUE_WINDOW_SECS = 7200  # 2 hours
_FATIGUE_MATCH_THRESHOLD = 3  # 3+ matches in window = fatigued
_FATIGUE_PENALTY = 0.88  # 12% confidence reduction
_FATIGUE_FRESH_BONUS = 1.03  # slight boost if well-rested (0 matches in 2h)

# Streak thresholds
_STREAK_WINDOW = 5  # last 5 matches
_HOT_STREAK_THRESHOLD = 4  # 4+ wins in last 5 = hot
_COLD_STREAK_THRESHOLD = 4  # 4+ losses in last 5 = cold
_HOT_STREAK_BONUS = 1.06
_COLD_STREAK_PENALTY = 0.90

# Goal variance thresholds
_MIN_MATCHES_FOR_VARIANCE = 5
_HIGH_VARIANCE_THRESHOLD = 3.5  # stdev of total goals
_LOW_VARIANCE_BONUS = 1.04  # consistent matchups are more predictable
_HIGH_VARIANCE_PENALTY = 0.92  # volatile matchups are less predictable


def _tod_bucket(unix_ts: int) -> int:
    """Convert a UTC timestamp to a time-of-day bucket index (0-5)."""
    hour = (unix_ts % 86400) // 3600
    return hour // 4


@dataclass
class MatchContext:
    """Contextual modifiers for a specific upcoming match.

    Each modifier defaults to 1.0 (neutral). Values < 1.0 reduce
    confidence, > 1.0 boost it. The combined modifier is the product
    of all individual factors.
    """

    # Time-of-day: based on both players' historical performance in this slot
    tod_modifier: float = 1.0
    tod_detail: str = ""

    # Fatigue: per-player, combined as average
    home_fatigue_modifier: float = 1.0
    away_fatigue_modifier: float = 1.0
    home_recent_match_count: int = 0
    away_recent_match_count: int = 0

    # Streaks: per-player momentum
    home_streak_modifier: float = 1.0
    away_streak_modifier: float = 1.0
    home_streak: int = 0  # positive = winning, negative = losing
    away_streak: int = 0

    # Goal variance: matchup consistency
    variance_modifier: float = 1.0
    goal_stdev: float = 0.0

    @property
    def fatigue_modifier(self) -> float:
        """Combined fatigue modifier (average of both players)."""
        return (self.home_fatigue_modifier + self.away_fatigue_modifier) / 2.0

    @property
    def streak_modifier(self) -> float:
        """Combined streak modifier (average of both players)."""
        return (self.home_streak_modifier + self.away_streak_modifier) / 2.0

    @property
    def combined_modifier(self) -> float:
        """Product of all context modifiers."""
        return (
            self.tod_modifier
            * self.fatigue_modifier
            * self.streak_modifier
            * self.variance_modifier
        )


class ContextAnalyzer:
    """Computes contextual modifiers for an upcoming match."""

    def __init__(self, db: MatchDatabase):
        self.db = db

    def analyze(
        self,
        home: str,
        away: str,
        start_time: int,
        league_id: int | None = None,
    ) -> MatchContext:
        """Compute all contextual factors for a matchup."""
        ctx = MatchContext()

        self._compute_tod(ctx, home, away, start_time, league_id)
        self._compute_fatigue(ctx, home, away, start_time, league_id)
        self._compute_streaks(ctx, home, away, league_id)
        self._compute_variance(ctx, home, away, league_id)

        logger.debug(
            "Context %s vs %s: tod=%.2f fatigue=%.2f streak=%.2f var=%.2f → %.2f",
            extract_handle(home), extract_handle(away),
            ctx.tod_modifier, ctx.fatigue_modifier,
            ctx.streak_modifier, ctx.variance_modifier,
            ctx.combined_modifier,
        )
        return ctx

    def _compute_tod(
        self,
        ctx: MatchContext,
        home: str,
        away: str,
        start_time: int,
        league_id: int | None,
    ) -> None:
        """Compare players' performance in this time slot vs overall."""
        bucket = _tod_bucket(start_time)
        bucket_label = _TOD_BUCKETS.get(bucket, "??")

        home_mod = self._player_tod_modifier(home, start_time, league_id)
        away_mod = self._player_tod_modifier(away, start_time, league_id)

        ctx.tod_modifier = (home_mod + away_mod) / 2.0
        ctx.tod_detail = f"UTC {bucket_label}"

    def _player_tod_modifier(
        self, player: str, start_time: int, league_id: int | None,
    ) -> float:
        """Compute a single player's time-of-day performance modifier.

        Compares their win rate in this time bucket to their overall win rate.
        If they perform significantly better or worse at this time, adjust.
        """
        matches = self.db.get_player_matches(player, limit=100, league_id=league_id)
        if len(matches) < 10:
            return 1.0

        target_bucket = _tod_bucket(start_time)

        overall_wins = sum(1 for m in matches if m.won_by(player))
        overall_rate = overall_wins / len(matches)

        bucket_matches = [m for m in matches if _tod_bucket(m.start_time) == target_bucket]
        if len(bucket_matches) < 5:
            return 1.0  # not enough data for this time slot

        bucket_wins = sum(1 for m in bucket_matches if m.won_by(player))
        bucket_rate = bucket_wins / len(bucket_matches)

        # Difference from overall performance
        diff = bucket_rate - overall_rate

        # Scale: +15% or more → 1.06 bonus, -15% or worse → 0.92 penalty
        # Linear interpolation between, capped at ±8%
        modifier = 1.0 + max(-0.08, min(0.06, diff * 0.50))
        return modifier

    def _compute_fatigue(
        self,
        ctx: MatchContext,
        home: str,
        away: str,
        start_time: int,
        league_id: int | None,
    ) -> None:
        """Check how many matches each player has had in the last 2 hours."""
        ctx.home_recent_match_count = self._count_recent_matches(
            home, start_time, league_id
        )
        ctx.away_recent_match_count = self._count_recent_matches(
            away, start_time, league_id
        )

        ctx.home_fatigue_modifier = self._fatigue_modifier(ctx.home_recent_match_count)
        ctx.away_fatigue_modifier = self._fatigue_modifier(ctx.away_recent_match_count)

    def _count_recent_matches(
        self, player: str, before_time: int, league_id: int | None,
    ) -> int:
        """Count matches played in the fatigue window before start_time."""
        cutoff = before_time - _FATIGUE_WINDOW_SECS
        matches = self.db.get_player_matches(player, limit=20, league_id=league_id)
        return sum(1 for m in matches if cutoff <= m.start_time < before_time)

    @staticmethod
    def _fatigue_modifier(match_count: int) -> float:
        """Convert recent match count to a fatigue modifier."""
        if match_count >= _FATIGUE_MATCH_THRESHOLD:
            return _FATIGUE_PENALTY
        if match_count == 0:
            return _FATIGUE_FRESH_BONUS
        return 1.0

    def _compute_streaks(
        self,
        ctx: MatchContext,
        home: str,
        away: str,
        league_id: int | None,
    ) -> None:
        """Check last N match W/L streak for each player."""
        ctx.home_streak, ctx.home_streak_modifier = self._player_streak(
            home, league_id
        )
        ctx.away_streak, ctx.away_streak_modifier = self._player_streak(
            away, league_id
        )

    def _player_streak(
        self, player: str, league_id: int | None,
    ) -> tuple[int, float]:
        """Compute current streak and its modifier.

        Returns (streak_value, modifier) where streak_value is positive
        for winning streaks, negative for losing streaks.
        """
        matches = self.db.get_player_matches(
            player, limit=_STREAK_WINDOW, league_id=league_id,
        )
        if len(matches) < _STREAK_WINDOW:
            return 0, 1.0

        wins = sum(1 for m in matches if m.won_by(player))
        losses = sum(1 for m in matches if not m.won_by(player) and not m.is_draw)

        # Compute consecutive streak from most recent
        streak = 0
        for m in matches:
            if m.won_by(player):
                if streak >= 0:
                    streak += 1
                else:
                    break
            elif not m.is_draw:
                if streak <= 0:
                    streak -= 1
                else:
                    break
            else:
                break  # draw breaks streak

        modifier = 1.0
        if wins >= _HOT_STREAK_THRESHOLD:
            modifier = _HOT_STREAK_BONUS
        elif losses >= _COLD_STREAK_THRESHOLD:
            modifier = _COLD_STREAK_PENALTY

        return streak, modifier

    def _compute_variance(
        self,
        ctx: MatchContext,
        home: str,
        away: str,
        league_id: int | None,
    ) -> None:
        """Compute goal total variance for this H2H matchup.

        Low variance = consistent, predictable totals → slight confidence boost.
        High variance = volatile, unpredictable → confidence penalty.
        """
        h2h = self.db.get_h2h_matches(home, away, limit=30)
        if len(h2h) < _MIN_MATCHES_FOR_VARIANCE:
            return

        totals = [m.total_goals for m in h2h]
        mean = sum(totals) / len(totals)
        variance = sum((t - mean) ** 2 for t in totals) / len(totals)
        stdev = math.sqrt(variance)

        ctx.goal_stdev = stdev

        if stdev >= _HIGH_VARIANCE_THRESHOLD:
            ctx.variance_modifier = _HIGH_VARIANCE_PENALTY
        elif stdev <= 1.5:
            ctx.variance_modifier = _LOW_VARIANCE_BONUS
        # else: neutral 1.0
