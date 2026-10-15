"""Credentials and paths come from the environment, never from a dotenv file."""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_trading.config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_SIGNALS_PATH,
    DEFAULT_TRADING_HOME,
    Settings,
    get_settings,
)


def test_ibkr_settings_come_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_HOST", "10.0.0.8")
    monkeypatch.setenv("IBKR_PORT", "4002")
    monkeypatch.setenv("IBKR_CLIENT_ID", "7")
    monkeypatch.setenv("IBKR_ACCOUNT", "PAPER-ACCOUNT")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.ibkr_host == "10.0.0.8"
    assert settings.ibkr_port == 4002
    assert settings.ibkr_client_id == 7
    assert settings.ibkr_account == "PAPER-ACCOUNT"
    assert "ibkr_paper" not in Settings.model_fields  # refused: paper_only
    assert Settings.model_config.get("env_file") is None


def test_defaults_when_environment_is_unset() -> None:
    settings = get_settings()

    assert settings.signals_path == DEFAULT_SIGNALS_PATH
    assert settings.database_path == DEFAULT_DATABASE_PATH
    assert settings.ibkr_host == "127.0.0.1"
    assert settings.ibkr_port == 4002
    assert settings.ibkr_client_id == 1
    assert settings.ibkr_account == ""
    assert "ibkr_paper" not in Settings.model_fields  # refused: paper_only
    assert settings.trading_home == DEFAULT_TRADING_HOME
    assert settings.base_currency == "HKD"


def test_empty_env_vars_keep_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNALS_PATH", "")
    monkeypatch.setenv("DATABASE_PATH", "")
    monkeypatch.setenv("IBKR_PORT", "")
    monkeypatch.setenv("IBKR_ACCOUNT", "")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.signals_path == DEFAULT_SIGNALS_PATH
    assert settings.database_path == DEFAULT_DATABASE_PATH
    assert settings.ibkr_port == 4002
    assert settings.ibkr_account == ""
    assert "ibkr_paper" not in Settings.model_fields  # refused: paper_only


def test_dotenv_file_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "IBKR_ACCOUNT=FROM_DOTENV\nIBKR_HOST=secret-host\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.ibkr_account == ""
    assert settings.ibkr_host == "127.0.0.1"


def test_env_example_lists_names_only() -> None:
    names: list[str] = []
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        assert separator == "="
        assert value == ""
        names.append(key)

    assert names == [
        "SIGNALS_PATH",
        "DATABASE_PATH",
        "IBKR_HOST",
        "IBKR_PORT",
        "IBKR_CLIENT_ID",
        "IBKR_ACCOUNT",
        "TRADING_HOME",
        "BASE_CURRENCY",
    ]


def test_gitignore_keeps_env_local() -> None:
    lines = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in lines
    assert ".env.*" in lines
    assert "!.env.example" in lines
