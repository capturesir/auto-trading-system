"""Runtime configuration.

Broker credentials and other secrets are read only from the process
environment. This module does not load a ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SIGNALS_PATH = Path("/workspace/invest/signals.json")
DEFAULT_DATABASE_PATH = Path("data/auto_trading.sqlite")


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
    ibkr_paper: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings built from the current environment."""
    return Settings()
