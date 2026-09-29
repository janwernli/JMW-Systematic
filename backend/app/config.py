"""Application settings, loaded from environment variables and an optional `.env` file.

Secrets (market-data keys) live only here, on the server side. They are never
returned by any API endpoint.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server
    host: str = Field(default="127.0.0.1", alias="APP_HOST")
    port: int = Field(default=8765, alias="APP_PORT")
    cors_origins: str = Field(
        default="http://127.0.0.1:5173,http://localhost:5173", alias="APP_CORS_ORIGINS"
    )

    # Storage
    database_path: Path = Field(default=REPO_ROOT / "data" / "momentum.db", alias="DATABASE_PATH")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "text"] = Field(default="json", alias="LOG_FORMAT")

    # Market data
    market_data_provider: Literal["alpaca", "norgate"] = Field(default="alpaca", alias="MARKET_DATA_PROVIDER")
    norgate_index: str = Field(default="Russell 1000", alias="NORGATE_INDEX")
    norgate_history_start: str = Field(default="2000-01-01", alias="NORGATE_HISTORY_START")
    alpaca_api_key_id: SecretStr | None = Field(default=None, alias="ALPACA_API_KEY_ID")
    alpaca_api_secret_key: SecretStr | None = Field(default=None, alias="ALPACA_API_SECRET_KEY")
    alpaca_data_feed: Literal["iex", "sip"] = Field(default="sip", alias="ALPACA_DATA_FEED")
    alpaca_data_base_url: str = Field(
        default="https://data.alpaca.markets", alias="ALPACA_DATA_BASE_URL"
    )
    alpaca_trading_base_url: str = Field(
        # Used ONLY to read the asset list (metadata). No order endpoints are called.
        default="https://paper-api.alpaca.markets",
        alias="ALPACA_TRADING_BASE_URL",
    )
    alpaca_universe_file: Path | None = Field(default=None, alias="ALPACA_UNIVERSE_FILE")
    alpaca_history_start: str = Field(default="2016-01-01", alias="ALPACA_HISTORY_START")
    alpaca_max_symbols: int = Field(default=600, alias="ALPACA_MAX_SYMBOLS")

    # SEC EDGAR (sector SIC codes): descriptive User-Agent with contact e-mail is required by the SEC
    sec_user_agent: str = Field(default="", alias="SEC_USER_AGENT")

    # Alpaca PAPER trading automation (orders go to the paper account only)
    broker_trading_enabled: bool = Field(default=False, alias="BROKER_TRADING_ENABLED")
    broker_paper_url: str = Field(default="https://paper-api.alpaca.markets", alias="ALPACA_PAPER_TRADING_URL")

    @field_validator("database_path", "alpaca_universe_file", mode="after")
    @classmethod
    def _resolve_from_repo_root(cls, v: Path | None) -> Path | None:
        # Relative paths in .env are relative to the project folder, not the backend process's cwd.
        return v if v is None or v.is_absolute() else (REPO_ROOT / v).resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
