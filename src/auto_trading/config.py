"""Runtime configuration.

Broker credentials and other secrets are read only from the process
environment. This module does not load a ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SIGNALS_PATH = Path("/workspace/invest/signals.json")
DEFAULT_DATABASE_PATH = Path("data/auto_trading.sqlite")
DEFAULT_TRADING_HOME = Path("/workspace/trading")


class Settings(BaseSettings):
    """Process configuration. Secrets have no baked-in account values."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_ignore_empty=True,
        extra="ignore",
    )

    signals_path: Path = Field(default=DEFAULT_SIGNALS_PATH)
    database_path: Path = Field(default=DEFAULT_DATABASE_PATH)
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4002
    ibkr_client_id: int = 1
    ibkr_account: str = ""
    trading_home: Path = Field(default=DEFAULT_TRADING_HOME)
    base_currency: Literal["HKD", "USD"] = "HKD"

    @field_validator("ibkr_port")
    @classmethod
    def _paper_port_only(cls, value: int) -> int:
        # 4002 Gateway and 7497 TWS. Other ports are refused: paper_only.
        if value not in (4002, 7497):
            raise ValueError("port refused: paper_only; only 4002 and 7497 are accepted")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings built from the current environment."""
    return Settings()
