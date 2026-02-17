"""Application configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration is loaded from env vars / .env file."""

    # BetsAPI
    betsapi_token: str = ""
    betsapi_base_url: str = "https://api.b365api.com/v3"

    # League IDs to track
    # 23114 = GT Leagues 12min, 37298 = GG League 8min, 38439 = Volta 6min
    league_ids: str = "23114,37298,38439"

    # Polling
    poll_interval: int = 120  # seconds

    # Edge detection thresholds
    min_edge_percent: float = 3.0  # minimum EV edge to alert on (%)
    min_odds_difference: float = 0.10  # minimum decimal odds gap across books
    clv_lookback_hours: int = 4  # hours of closing line value history

    # Steam move detection
    steam_move_threshold: float = 0.05  # decimal odds shift to flag as steam
    steam_move_window_seconds: int = 300  # 5 minute window

    # Alerts
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Data persistence
    data_dir: str = "data"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def tracked_league_ids(self) -> list[int]:
        return [int(lid.strip()) for lid in self.league_ids.split(",") if lid.strip()]


settings = Settings()
