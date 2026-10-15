"""Runtime configuration.

Host, port, and the expected account come from the process environment.
This module does not load a ``.env`` file and does not read Gateway login
names. Those belong to the separate IBC launcher.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from auto_trading.paper_account import DU_ACCOUNT

DEFAULT_SIGNALS_PATH = Path("/workspace/invest/signals.json")
DEFAULT_DATABASE_PATH = Path("data/auto_trading.sqlite")
DEFAULT_TRADING_HOME = Path("/workspace/trading")
_REMOVED_APPROVAL_SWITCH = "PAPER_AUTO_APPROVE"


def manual_forced(value: str | bool | None) -> bool:
    """True only for the boolean or the text ``true``. Other values stay off."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def assert_startup_switches(
    env: Mapping[str, str] | None = None,
    config_text: str = "",
    config_path: Path | None = None,
) -> None:
    """Refuse a process that still names the removed approval switch."""
    current = os.environ if env is None else env
    text = config_text
    if config_path is not None and config_path.is_file():
        text += config_path.read_text(encoding="utf-8")
    if _REMOVED_APPROVAL_SWITCH in current or _REMOVED_APPROVAL_SWITCH in text:
        raise SystemExit(f"{_REMOVED_APPROVAL_SWITCH} was removed and cannot enable approval")


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
    force_manual_approval: str = Field(
        default="false",
        validation_alias=AliasChoices("FORCE_MANUAL_APPROVAL"),
    )

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
    assert_startup_switches()
    return Settings()
