"""Shared fixtures. Settings are rebuilt from a cleaned environment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from auto_trading.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"

ENV_VARS = (
    "SIGNALS_PATH",
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "IB_CLIENT_ID",
    "IBKR_PAPER_ACCOUNT",
    "IBKR_PAPER_USERNAME",
    "IBKR_PAPER_PASSWORD",
    "IB_CLIENT_ID_EXEC",
    "IB_CLIENT_ID_RECON",
    "IB_CLIENT_ID_REPORT",
    "IB_HOST",
    "IB_PORT",
    "IB_ACCOUNT",
    "DATABASE_PATH",
    "TRADING_HOME",
    "BASE_CURRENCY",
    "FORCE_MANUAL_APPROVAL",
    "PAPER_AUTO_APPROVE",
    "APPROVER",
    "NOTIFIER",
    "HEALTHCHECK_PING_URL",
    "IB_CLIENT_ID_HOLDINGS",
    "IB_READ_ONLY",
    "HOLDINGS_MONITOR_INTERVAL_SEC",
    "HOLDINGS_SNAPSHOT_PATH",
    "HOLDINGS_REALERT_WARN_SEC",
    "HOLDINGS_REALERT_CRIT_SEC",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def valid_payload() -> dict:
    return json.loads((FIXTURES / "valid.json").read_text(encoding="utf-8"))
