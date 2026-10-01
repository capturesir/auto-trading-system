"""Runtime configuration.

Broker credentials and other secrets are read only from the process
environment. This module does not load a ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
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
    ibkr_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("IBKR_HOST", "IB_HOST"),
    )
    ibkr_port: int = Field(
        default=4002,
        validation_alias=AliasChoices("IBKR_PORT", "IB_PORT"),
    )
    ibkr_client_id: int = 1
    ib_client_id_exec: int = 11
    ib_client_id_recon: int = 12
    ib_client_id_report: int = 13
    ibkr_account: str = Field(
        default="",
        validation_alias=AliasChoices("IBKR_ACCOUNT", "IB_ACCOUNT"),
    )
    ibkr_paper_username: str = ""
    ibkr_paper_password: SecretStr = SecretStr("")
    trading_home: Path = Field(default=DEFAULT_TRADING_HOME)
    base_currency: Literal["HKD", "USD"] = "HKD"

    @field_validator("ibkr_port")
    @classmethod
    def _paper_port_only(cls, value: int) -> int:
        # 4002 Gateway and 7497 TWS. Other ports are refused: paper_only.
        if value not in (4002, 7497):
            raise ValueError("port refused: paper_only; only 4002 and 7497 are accepted")
        return value

    @field_validator("ibkr_account")
    @classmethod
    def _paper_account_only(cls, value: str) -> str:
        if value and not value.startswith("DU"):
            raise ValueError("account refused: paper_only, expected account must start with DU")
        return value


def require_paper_credentials(settings: Settings) -> None:
    """Fail closed before a real Gateway connect. Values are never included."""
    username = settings.ibkr_paper_username
    password = settings.ibkr_paper_password.get_secret_value()
    if not username or not password:
        raise RuntimeError("missing IBKR_PAPER_USERNAME or IBKR_PAPER_PASSWORD")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings built from the current environment."""
    return Settings()
