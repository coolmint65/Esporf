"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration is loaded from env vars / .env file."""

    # BetsAPI
    betsapi_token: str = ""
    betsapi_base_url: str = "https://api.b365api.com/v3"

    # League IDs to track (2025 season)
    # 42648 = Esoccer Battle 8min (2025), 42649 = GT Leagues 12min (2025), 38439 = Volta 6min
    league_ids: str = "42648,42649,38439"

    # Display timezone (e.g. "US/Eastern", "UTC")
    timezone: str = "US/Eastern"

    # Polling & schedule
    poll_interval: int = 180  # seconds (3 min) — GG/GT League matches are longer than Volta
    schedule_lookahead: int = 1800  # seconds (30 min) — how far ahead to show/alert matches

    # Trend detection thresholds
    min_hit_rate: float = 0.70  # 70% minimum hit rate to surface a trend
    min_sample_size: int = 10  # minimum matches needed to consider a trend valid
    last_n_matches: int = 20  # how many recent matches to analyze per player/H2H

    # Over/under goal lines to check
    goal_lines: str = "2.5,3.5,4.5,5.5,6.5,7.5"

    # Realistic lines the sportsbook actually offers for Volta
    # Used as fallback when real odds aren't available
    volta_book_lines: str = "2.5,3.5,4.5"

    # Alerts
    discord_webhook_url: str = ""
    discord_role_id: str = ""  # Role ID to mention in alerts (e.g. "eSoccer" role)

    # Discord bot (alternative to webhook alerts)
    discord_bot_token: str = ""
    discord_channel_id: str = ""  # Channel ID where bot sends alerts

    # Data persistence
    data_dir: str = "data"
    db_path: str = "data/esporf.db"

    # History backfill — pages of ended matches to fetch per league on first run
    backfill_pages: int = 10

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def tracked_league_ids(self) -> list[int]:
        return [int(lid.strip()) for lid in self.league_ids.split(",") if lid.strip()]

    @property
    def goal_line_values(self) -> list[float]:
        return [float(v.strip()) for v in self.goal_lines.split(",") if v.strip()]

    @property
    def volta_book_line_values(self) -> list[float]:
        return [float(v.strip()) for v in self.volta_book_lines.split(",") if v.strip()]


settings = Settings()
