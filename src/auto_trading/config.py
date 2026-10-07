"""Runtime configuration.

Host, port, and the expected account come from the process environment.
This module does not load a ``.env`` file and does not read Gateway login
names. Those belong to the separate IBC launcher.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from auto_trading.paper_account import DU_ACCOUNT

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
        validation_alias=AliasChoices("IB_HOST", "IBKR_HOST"),
    )
    ibkr_port: int = Field(
        default=4002,
        validation_alias=AliasChoices("IB_PORT", "IBKR_PORT"),
    )
    ibkr_client_id: int = Field(
        default=1,
        validation_alias=AliasChoices("IB_CLIENT_ID", "IBKR_CLIENT_ID"),
    )
    ib_client_id_exec: int = 11
    ib_client_id_recon: int = 12
    ib_client_id_report: int = 13
    ibkr_paper_account: str = Field(
        default="",
        validation_alias=AliasChoices("IBKR_PAPER_ACCOUNT"),
    )
    trading_home: Path = Field(default=DEFAULT_TRADING_HOME)
    base_currency: Literal["HKD", "USD"] = "HKD"

    @field_validator("ibkr_port")
    @classmethod
    def _paper_port_only(cls, value: int) -> int:
        # 4002 Gateway and 7497 TWS. Other ports are refused: paper_only.
        if value not in (4002, 7497):
            raise ValueError("port refused: paper_only; only 4002 and 7497 are accepted")
        return value

    @field_validator("ibkr_paper_account")
    @classmethod
    def _paper_account_only(cls, value: str) -> str:
        # ``DU``, one optional letter, then digits. Other ids are refused: paper_only.
        value = value.strip()
        if value and DU_ACCOUNT.fullmatch(value) is None:
            raise ValueError("account refused: paper_only")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings built from the current environment."""
    return Settings()
