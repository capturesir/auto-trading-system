"""NLV from a paper account whose NetLiquidation tag is the real base currency."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from auto_trading.execution.fx import DEFAULT_RATES_HKD, rates_from_account_values
from auto_trading.execution.ibkr import net_liquidation_base
from auto_trading.execution.services import account_once

PIN = "DUR243736"
GATE_NOW = datetime(2026, 10, 2, 22, 0, tzinfo=ZoneInfo("Asia/Macau"))


def _paper_usd_rows(value: str = "1000") -> list[dict[str, str]]:
    """DUR243736 shape: NetLiquidation in USD, ledger total tagged BASE."""
    return [
        {"tag": "NetLiquidation", "currency": "USD", "value": value},
        {"tag": "$LEDGER-NetLiquidationByCurrency", "currency": "BASE", "value": value},
        {"tag": "ExchangeRate", "currency": "USD", "value": "1"},
        {"tag": "ExchangeRate", "currency": "BASE", "value": "1"},
    ]


def test_usd_only_net_liquidation_when_account_base_is_usd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    minimal = [
        {"tag": "NetLiquidation", "currency": "USD", "value": "1000"},
        {"tag": "$LEDGER-NetLiquidationByCurrency", "currency": "BASE", "value": "1000"},
    ]
    nlv = net_liquidation_base(minimal)
    assert nlv == pytest.approx(1000 * DEFAULT_RATES_HKD["USD"])
    rows = _paper_usd_rows()
    assert net_liquidation_base(rows) == pytest.approx(nlv)
    quoted = [
        *rows,
        {"tag": "ExchangeRate", "currency": "HKD", "value": "0.125"},
    ]
    assert net_liquidation_base(quoted) == pytest.approx(8000.0)
    quoted_rates = rates_from_account_values(quoted)
    assert quoted_rates["USD"] == pytest.approx(8.0)
    assert "BASE" not in quoted_rates

    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    result = account_once(
        tmp_path,
        GATE_NOW,
        snapshot={
            "accounts": [PIN],
            "nlv": nlv,
            "positions": [],
            "open_orders": [],
        },
    )
    assert result["status"] == "wrote"
    assert result["wrote"] is True
    saved = json.loads((tmp_path / "state" / "account.json").read_text(encoding="utf-8"))
    assert saved["nlv"] == pytest.approx(nlv)
    assert saved["account"] == PIN


def test_base_tag_still_accepted() -> None:
    rows = [
        {"tag": "NetLiquidation", "currency": "USD", "value": "100"},
        {"tag": "NetLiquidation", "currency": "HKD", "value": "780"},
    ]
    assert net_liquidation_base(rows) is None
    rows.append({"tag": "NetLiquidation", "currency": "BASE", "value": "780"})
    assert net_liquidation_base(rows) == 780.0
    assert (
        net_liquidation_base([{"tag": "NetLiquidation", "currency": "BASE", "value": "780"}])
        == 780.0
    )
    assert (
        net_liquidation_base(
            [{"tag": "$LEDGER-NetLiquidationByCurrency", "currency": "BASE", "value": "780"}]
        )
        == 780.0
    )
    # Known USD base: the BASE alias is account-base USD and converts to HKD.
    usd_base = [
        {"tag": "NetLiquidation", "currency": "BASE", "value": "1000"},
        {"tag": "ExchangeRate", "currency": "USD", "value": "1"},
        {"tag": "ExchangeRate", "currency": "HKD", "value": "0.125"},
    ]
    assert net_liquidation_base(usd_base) == pytest.approx(8000.0)


def test_missing_nlv_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert net_liquidation_base([]) is None
    assert (
        net_liquidation_base([{"tag": "TotalCashValue", "currency": "BASE", "value": "10"}]) is None
    )
    assert (
        net_liquidation_base([{"tag": "NetLiquidation", "currency": "EUR", "value": "1000"}])
        is None
    )
    broken = _paper_usd_rows("not-a-number")
    assert net_liquidation_base(broken) is None
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", PIN)
    result = account_once(
        tmp_path,
        GATE_NOW,
        snapshot={"accounts": [PIN], "nlv": None, "positions": [], "open_orders": []},
    )
    assert result == {"status": "nlv_unavailable", "wrote": False}
    assert not (tmp_path / "state" / "account.json").exists()
