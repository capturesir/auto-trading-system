"""Daily fill summary written at 06:00 Asia/Macau."""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auto_trading.execution.approval_mode import approver_name
from auto_trading.execution.breakers import flags, risk_window
from auto_trading.portfolio.journal import TradeJournal
from auto_trading.reports.benchmarks import build_benchmarks, write_monthly_review

MACAU = timezone(timedelta(hours=8))
SCHEMA = "daily_summary/1.1"
MARKETS = ("US", "HK", "JP", "KR", "TW")
REQUIRED = (
    "schema_version",
    "risk_date",
    "window_start",
    "window_end",
    "generated_at",
    "status",
    "revision",
    "account_suffix",
    "trading_scope",
    "base_currency",
    "approvals",
    "fills",
    "rejections",
    "rejection_counts",
    "positions",
    "pnl",
    "nlv_base",
    "hwm_base",
    "drawdown_pct",
    "r15",
    "halts",
    "by_quote_type",
    "by_market",
    "benchmarks",
)


def validate_daily_summary(payload: dict[str, Any]) -> None:
    """Raise ValueError when a required daily_summary/1.1 field is missing."""
    if payload.get("schema_version") != SCHEMA:
        raise ValueError("schema")
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError("missing " + ",".join(missing))
    if payload["status"] not in {"provisional", "final"}:
        raise ValueError("status")
    halts = payload["halts"]
    for key in (
        "kill_switch",
        "r8_active",
        "r9_triggered",
        "r29_active",
        "r29_anomaly_count",
        "r30_active",
        "critical_events",
        "r9_cycle_state",
        "r9_size_mult",
        "r9_cooldown_days_done",
        "r9_half_size_days_done",
    ):
        if key not in halts:
            raise ValueError(key)
    for market in MARKETS:
        if market not in payload["by_market"]:
            raise ValueError(market)
    approvals = payload["approvals"]
    for key in (approver_name(), "user", "reapproved", "chase_reapproval_exceeded", "expired"):
        if key not in approvals:
            raise ValueError("approvals")
    block = payload["benchmarks"]
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError("benchmarks")
    for key in ("paper_start_date", "us_trade_date", "paper", "items"):
        if key not in block:
            raise ValueError("benchmarks")


def _market(symbol: str) -> str:
    if symbol.endswith(".HK"):
        return "HK"
    if symbol.endswith(".T"):
        return "JP"
    if symbol.endswith(".TW"):
        return "TW"
    if symbol.endswith(".KS") or (symbol.isdigit() and len(symbol) == 6):
        return "KR"
    return "US"


def _quote_bucket(value: str | None, market_data_type: int | None) -> str:
    if value in {"realtime", "delayed"}:
        return value
    if market_data_type == 1:
        return "realtime"
    if market_data_type in {3, 4}:
        return "delayed"
    return "delayed" if value == "delayed_frozen" else "realtime"


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# 每日成交摘要 {payload['risk_date']}",
        "",
        f"- 狀態：{payload['status']}（修訂 {payload['revision']}）",
        f"- 區間：{payload['window_start']} 至 {payload['window_end']}",
        f"- 成交 {len(payload['fills'])} 筆，拒絕 {len(payload['rejections'])} 筆",
        f"- R15 已送出 {payload['r15']['submitted']} / {payload['r15']['limit']}",
        f"- 損益合計 {payload['pnl']['total_base']}",
        "",
        "| 市場 | 成交 | 拒絕 |",
        "| --- | --- | --- |",
    ]
    for market in MARKETS:
        row = payload["by_market"][market]
        lines.append(f"| {market} | {row['fills']} | {row['rejections']} |")
    lines.append("")
    block = payload.get("benchmarks")
    if not block:
        lines.append("基準比較尚未開始")
        lines.append("")
        return "\n".join(lines) + "\n"
    lines.append("基準比較")
    for item in block.get("items") or []:
        lines.append(f"- {item.get('id')} {item.get('status')} {item.get('na_reason')}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _r9_summary(trading_home: Path) -> dict[str, Any]:
    from auto_trading.risk.r9_state import summary_fields

    return summary_fields(trading_home)


def write_daily_summary(
    trading_home: Path,
    *,
    now: datetime,
    complete: bool,
    pin: str = "",
    r15_limit: int = 20,
    benchmark_get: Any = None,
    benchmark_clock: Any = None,
) -> dict[str, Any]:
    """Write JSON and Markdown for the risk day that contains ``now``."""
    start, end, risk_date = risk_window(now)
    local = now.astimezone(MACAU)
    journal = TradeJournal(trading_home)
    fills_raw = journal.summary_fills(start, end)
    rejections_raw = journal.summary_rejections(start, end)
    submitted = journal.summary_submitted(start, end)
    approval_counts = journal.summary_approvals(start, end)
    state = flags(trading_home)
    account_path = trading_home / "state" / "account.json"
    account: dict[str, Any] = {}
    if account_path.is_file():
        account = json.loads(account_path.read_text(encoding="utf-8"))
    fills = []
    for row in fills_raw:
        symbol = str(row["symbol"])
        bucket = _quote_bucket(row.get("quote_data_type"), row.get("market_data_type"))
        fills.append(
            {
                "order_id": row["order_id"],
                "market": _market(symbol),
                "ticker": symbol,
                "side": row.get("side") or "BUY",
                "qty": row["quantity"],
                "avg_fill_price": row["price"],
                "currency": row.get("currency") or "HKD",
                "notional_base": row.get("notional_base"),
                "strategy_id": row.get("strategy_id"),
                "approver": row.get("approver"),
                "quote_data_type": bucket,
                "delay_sec": row.get("delay_sec"),
                "slippage_bps_vs_ref": row.get("slippage_bps_vs_ref"),
                "slippage_bps_vs_official_open": row.get("slippage_bps_vs_official_open"),
            }
        )
    rejections = []
    for row in rejections_raw:
        detail = row.get("detail") or {}
        market_data_type = detail.get("market_data_type")
        label = market_data_type
        if market_data_type == 1:
            label = "realtime"
        elif market_data_type in {3, 4}:
            label = "delayed"
        rejections.append(
            {
                "intent_id": row.get("intent_id"),
                "market": detail.get("market") or _market(str(row.get("symbol") or "")),
                "ticker": row.get("symbol"),
                "stage": detail.get("stage"),
                "rule": detail.get("rule") or row.get("code"),
                "reason_code": row.get("code"),
                "market_data_type": label,
            }
        )
    rejection_counts: dict[str, int] = {}
    for row in rejections:
        code = str(row["reason_code"] or "")
        rejection_counts[code] = rejection_counts.get(code, 0) + 1
    by_market = {market: {"fills": 0, "rejections": 0} for market in MARKETS}
    realtime_slip: list[float] = []
    delayed_slip: list[float] = []
    delayed_delay: list[float] = []
    realtime_fills = delayed_fills = 0
    for row in fills:
        by_market[row["market"]]["fills"] += 1
        if row["quote_data_type"] == "delayed":
            delayed_fills += 1
            if row["slippage_bps_vs_ref"] is not None:
                delayed_slip.append(float(row["slippage_bps_vs_ref"]))
            if row["delay_sec"] is not None:
                delayed_delay.append(float(row["delay_sec"]))
        else:
            realtime_fills += 1
            if row["slippage_bps_vs_ref"] is not None:
                realtime_slip.append(float(row["slippage_bps_vs_ref"]))
    realtime_rej = delayed_rej = 0
    for row in rejections:
        market = row["market"] if row["market"] in by_market else "US"
        by_market[market]["rejections"] += 1
        if row["market_data_type"] == "delayed":
            delayed_rej += 1
        else:
            realtime_rej += 1
    nlv = float(account.get("nlv") or 0)
    hwm = float(account.get("hwm") or 0)
    realized = float(account.get("realized_base") or 0)
    strategy_rows = journal.strategy_position_rows()
    if strategy_rows:
        positions = [
            {
                "ticker": row["ticker"],
                "strategy_id": row["strategy_id"],
                "qty": row["qty"],
                "market_value_base": row["avg_cost"],
                "unrealized_pnl_base": 0.0,
            }
            for row in strategy_rows
            if float(row["qty"])
        ]
    else:
        positions = account.get("positions") or []
    unrealized = sum(float(row.get("unrealized_pnl_base") or 0) for row in positions)
    name = f"summary_{start.strftime('%Y%m%d')}.json"
    folder = trading_home / "reports" / "daily"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    revision = 1
    if target.is_file():
        previous = json.loads(target.read_text(encoding="utf-8"))
        revision = int(previous.get("revision") or 1) + 1
    critical_count = 0
    log_dir = trading_home / "logs"
    if log_dir.is_dir():
        for path in log_dir.glob("critical_*.jsonl"):
            lines = path.read_text(encoding="utf-8").splitlines()
            critical_count += sum(1 for line in lines if line)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA,
        "risk_date": risk_date,
        "window_start": start.isoformat(timespec="seconds"),
        "window_end": end.isoformat(timespec="seconds"),
        "generated_at": local.isoformat(timespec="seconds"),
        "status": "final" if complete else "provisional",
        "revision": revision,
        "account_suffix": pin[-4:] if len(pin) >= 4 else pin,
        "trading_scope": "paper_only",
        "base_currency": "HKD",
        "approvals": {
            approver_name(): approval_counts.get("auto", 0),
            "user": approval_counts.get("user", 0),
            "reapproved": approval_counts.get("reapproved", 0),
            "chase_reapproval_exceeded": approval_counts.get("chase", 0),
            "expired": approval_counts.get("expired", 0),
        },
        "fills": fills,
        "rejections": rejections,
        "rejection_counts": rejection_counts,
        "positions": positions,
        "pnl": {
            "realized_base": realized,
            "unrealized_base": unrealized,
            "total_base": realized + unrealized,
            "pct_of_nlv": (realized + unrealized) / nlv if nlv else 0.0,
        },
        "nlv_base": nlv,
        "hwm_base": hwm,
        "drawdown_pct": (nlv / hwm - 1) if hwm else 0.0,
        "r15": {"submitted": submitted, "limit": r15_limit},
        "halts": {
            "kill_switch": (trading_home / "KILL_SWITCH").is_file(),
            "r8_active": bool(account.get("r8_active")),
            "r9_triggered": bool(account.get("r9_triggered")),
            "r29_active": bool(state.get("r29_active")),
            "r29_anomaly_count": int(state.get("r29_count") or 0),
            "r30_active": bool(state.get("r30_active")),
            "critical_events": critical_count,
            **_r9_summary(trading_home),
        },
        "by_quote_type": {
            "realtime": {
                "fills": realtime_fills,
                "rejections": realtime_rej,
                "median_slippage_bps_vs_ref": _median(realtime_slip),
            },
            "delayed": {
                "fills": delayed_fills,
                "rejections": delayed_rej,
                "median_slippage_bps_vs_ref": _median(delayed_slip),
                "median_delay_sec": _median(delayed_delay),
            },
        },
        "by_market": by_market,
        "benchmarks": _benchmarks(
            trading_home,
            now,
            account,
            benchmark_get=benchmark_get,
            benchmark_clock=benchmark_clock,
        ),
    }
    if local.day == 1:
        previous = (local.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        write_monthly_review(trading_home, previous, now=now)
    validate_daily_summary(payload)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    md = target.with_suffix(".md")
    md_tmp = md.with_suffix(".md.tmp")
    md_tmp.write_text(_markdown(payload), encoding="utf-8")
    os.replace(md_tmp, md)
    stamp = local.strftime("%Y%m%dT%H%M%S")
    outbox = trading_home / "notifications" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    note = outbox / f"{stamp}_daily_summary.json"
    note_tmp = note.with_suffix(".json.tmp")
    note_tmp.write_text(
        json.dumps(
            {
                "schema_version": "notice/1.0",
                "kind": "daily_summary",
                "level": "info",
                "path": str(target),
                "fills": len(fills),
                "rejections": len(rejections),
                "r15_submitted": submitted,
                "status": payload["status"],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(note_tmp, note)
    return payload


def _benchmark_na(trading_home: Path, now: datetime, exc: Exception) -> None:
    """Record a comparison failure. The summary still writes."""
    local = now.astimezone(MACAU)
    path = trading_home / "logs" / f"events_{local.strftime('%Y%m%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "event": "benchmark_na",
            "na_reason": "benchmark_error",
            "error": type(exc).__name__,
        },
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _benchmarks(
    trading_home: Path,
    now: datetime,
    account: dict[str, Any],
    *,
    benchmark_get: Any,
    benchmark_clock: Any,
) -> dict[str, Any] | None:
    """Comparison block. A fetch failure is recorded and does not stop the summary."""
    try:
        return build_benchmarks(
            trading_home,
            now,
            account,
            get=benchmark_get,
            clock=benchmark_clock,
        )
    except Exception as exc:
        _benchmark_na(trading_home, now, exc)
        return {
            "paper_start_date": None,
            "us_trade_date": None,
            "paper": None,
            "items": [],
            "status": "n/a",
            "na_reason": "benchmark_error",
        }
