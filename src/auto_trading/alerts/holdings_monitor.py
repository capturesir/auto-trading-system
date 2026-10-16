"""30-second holdings monitor. Alerts only. Paper account DUR243736.

This process does not place orders and does not write intent files.
Intraday ENTRY and EXIT intents belong to ``auto_trading.intraday``.

Client id 15 is the only id this process may use. 11 (submit), 12 (recon),
13 (account), 14 (watchdog), and 31 (health) are refused before any Gateway
connect. The process writes ``alerts/pending/`` and ``state/``. It does not
write intents or place orders.

A-group reads ``/workspace/invest/b-group/holdings_snapshot.json``, a symlink
to the paper-home copy of ``state/holdings_snapshot.json``. The VPS canonical
file is ``/srv/trading/state/holdings_snapshot.json``.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from auto_trading.alerts.holdings_alert_io import write_hm_alert
from auto_trading.alerts.holdings_positions import (
    PositionLoad,
    PositionView,
    broker_stop_for,
    load_broker_stops,
    load_positions,
    snapshot_age_sec,
    write_holdings_snapshot,
)
from auto_trading.alerts.holdings_state import (
    MACAU,
    as_macau,
    iso_macau,
    load_state,
    parse_moment,
    save_state,
    stamp,
)
from auto_trading.alerts.holdings_ta import (
    SESSION_RULES,
    AlertDraft,
    BarContext,
    MarketSnap,
    apply_status_severity,
    bars_from_rows,
    evaluate_position,
    market_open,
)
from auto_trading.alerts.pending import build_alert, dedup_key, make_alert_id
from auto_trading.paper_account import DU_ACCOUNT
from auto_trading.paper_only import port_accepted
from auto_trading.risk.r9_cycle import HALF, HALTED

log = logging.getLogger(__name__)

PAPER_ACCOUNT = "DUR243736"
REQUIRED_CLIENT_ID = 15
FORBIDDEN_CLIENT_IDS = frozenset({11, 12, 13, 14, 31})
MAX_ALERTS = 20
DATA_ERROR_COOLDOWN_SEC = 3600
EMPTY_SNAPSHOT_SEC = 60
READ_ONLY_VALUES = frozenset({"1", "true", "yes"})


class HoldingsMonitorError(Exception):
    """Startup configuration was refused. The Gateway is not contacted."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MonitorConfig:
    def __init__(
        self,
        trading_home: Path,
        *,
        interval_sec: float = 30,
        client_id: int = REQUIRED_CLIENT_ID,
        account: str = PAPER_ACCOUNT,
        read_only: bool = True,
        snapshot_path: Path | None = None,
        warn_sec: float = 300,
        crit_sec: float = 120,
        port: int = 4002,
    ) -> None:
        self.trading_home = trading_home
        self.interval_sec = interval_sec
        self.client_id = client_id
        self.account = account
        self.read_only = read_only
        self.snapshot_path = snapshot_path
        self.warn_sec = warn_sec
        self.crit_sec = crit_sec
        self.port = port


def parse_client_id(raw: str | None) -> int:
    """Refuse a missing id and every id reserved by another service."""
    if raw is None or not str(raw).strip():
        raise HoldingsMonitorError("client_id_missing")
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise HoldingsMonitorError("client_id_forbidden") from exc
    if value in FORBIDDEN_CLIENT_IDS or value != REQUIRED_CLIENT_ID:
        raise HoldingsMonitorError("client_id_forbidden")
    return value


def parse_account(raw: str | None) -> str:
    account = (raw or "").strip()
    if account != PAPER_ACCOUNT or DU_ACCOUNT.fullmatch(account) is None:
        raise HoldingsMonitorError("paper_account_rejected")
    return account


def parse_read_only(raw: str | None) -> bool:
    if str(raw or "").strip().lower() not in READ_ONLY_VALUES:
        raise HoldingsMonitorError("read_only_required")
    return True


def parse_port(raw: str | None) -> int:
    text = (raw or "4002").strip()
    try:
        port = int(text)
    except ValueError as exc:
        raise HoldingsMonitorError("port_rejected") from exc
    if not port_accepted(port):
        raise HoldingsMonitorError("port_rejected")
    return port


def _positive_float(raw: str | None, default: str, code: str) -> float:
    text = default if raw is None or not str(raw).strip() else str(raw).strip()
    try:
        value = float(text)
    except ValueError as exc:
        raise HoldingsMonitorError(code) from exc
    if value <= 0:
        raise HoldingsMonitorError(code)
    return value


def config_from_env(trading_home: Path | None = None) -> MonitorConfig:
    """Read the process environment. A bad pin refuses startup."""
    home_text = os.environ.get("TRADING_HOME", "").strip()
    home = trading_home or Path(home_text or "/srv/trading")
    snapshot_raw = os.environ.get("HOLDINGS_SNAPSHOT_PATH", "").strip()
    snapshot = Path(snapshot_raw) if snapshot_raw else None
    if snapshot is not None and not snapshot.is_absolute():
        snapshot = home / snapshot
    return MonitorConfig(
        home,
        interval_sec=_positive_float(
            os.environ.get("HOLDINGS_MONITOR_INTERVAL_SEC"),
            "30",
            "interval_invalid",
        ),
        client_id=parse_client_id(os.environ.get("IB_CLIENT_ID_HOLDINGS")),
        account=parse_account(os.environ.get("IBKR_PAPER_ACCOUNT")),
        read_only=parse_read_only(os.environ.get("IB_READ_ONLY")),
        snapshot_path=snapshot,
        warn_sec=_positive_float(
            os.environ.get("HOLDINGS_REALERT_WARN_SEC"),
            "300",
            "interval_invalid",
        ),
        crit_sec=_positive_float(
            os.environ.get("HOLDINGS_REALERT_CRIT_SEC"),
            "120",
            "interval_invalid",
        ),
        port=parse_port(os.environ.get("IB_PORT", os.environ.get("IBKR_PORT"))),
    )


def account_status(trading_home: Path) -> str:
    """Operator status copied onto each alert. The loop keeps running."""
    if (trading_home / "KILL_SWITCH").is_file():
        return "kill_switch"
    try:
        from auto_trading.risk.r9_state import cycle_view

        view = cycle_view(trading_home)
    except (OSError, ValueError):
        view = None
    if view is not None and view.get("state") in {HALTED, HALF}:
        return "r9_halt"
    if (trading_home / "state" / "paused").is_file():
        return "paused"
    return "running"


def wait_seconds(elapsed: float, interval: float) -> tuple[float, bool]:
    """Seconds to sleep, and whether this tick already used the whole interval."""
    if elapsed > interval:
        return 0.0, True
    return interval - elapsed, False


def _open_positions(positions: list[PositionView]) -> list[PositionView]:
    return [item for item in positions if abs(item.qty) > 1e-9]


def _monitor_key(draft: AlertDraft, trading_date: str, strategy_id: str | None) -> str:
    key = dedup_key(draft.rule_id, draft.ticker, draft.direction, trading_date)
    if strategy_id:
        return f"{key}|{strategy_id}"
    return key


def _alert_bucket(state: dict[str, Any], ticker: str) -> dict[str, Any]:
    per = state.setdefault("per_ticker", {})
    if not isinstance(per, dict):
        per = {}
        state["per_ticker"] = per
    body = per.setdefault(ticker, {})
    if not isinstance(body, dict):
        body = {}
        per[ticker] = body
    last = body.setdefault("last_alert", {})
    if not isinstance(last, dict):
        last = {}
        body["last_alert"] = last
    return last


def _should_emit(
    previous: dict[str, Any] | None,
    severity: str,
    now: datetime,
    *,
    warn_sec: float,
    crit_sec: float,
) -> bool:
    if not isinstance(previous, dict) or not previous.get("in_breach"):
        return True
    seen = parse_moment(previous.get("at"))
    if seen is None:
        return True
    elapsed = (now - seen).total_seconds()
    severe = severity == "critical" or previous.get("severity") == "critical"
    limit = crit_sec if severe else warn_sec
    return elapsed >= limit


def _mark(bucket: dict[str, Any], key: str, now: datetime, severity: str, *, emitted: bool) -> None:
    previous = bucket.get(key)
    if emitted or not isinstance(previous, dict):
        bucket[key] = {"at": iso_macau(now), "severity": severity, "in_breach": True}
        return
    previous["in_breach"] = True
    previous["severity"] = severity


def _clear_quiet(state: dict[str, Any], active: set[tuple[str, str]]) -> None:
    per = state.get("per_ticker")
    if not isinstance(per, dict):
        return
    for ticker, body in per.items():
        if not isinstance(body, dict):
            continue
        last = body.get("last_alert")
        if not isinstance(last, dict):
            continue
        for key, row in last.items():
            if (str(ticker), str(key)) in active or not isinstance(row, dict):
                continue
            row["in_breach"] = False


def _bars_for(
    home: Path,
    ticker: str,
    injected: Mapping[str, BarContext] | None,
) -> BarContext | None:
    if injected is not None and ticker in injected:
        return injected[ticker]
    if injected is not None:
        return None
    try:
        from auto_trading.market.bars import read_parquet
        from auto_trading.market.yahoo import cache_path

        path = cache_path(home / "data", ticker)
        if not path.is_file():
            return None
        return bars_from_rows(read_parquet(path))
    except (OSError, ValueError, ImportError):
        return None


def _as_snap(value: Any) -> MarketSnap | None:
    if isinstance(value, MarketSnap):
        return value
    last = getattr(value, "last", None)
    if last is None:
        return None
    try:
        price = float(last)
    except (TypeError, ValueError):
        return None
    return MarketSnap(
        last=price,
        source=str(getattr(value, "source", "ibkr") or "ibkr"),
        delay_sec=getattr(value, "delay_sec", None),
        open_price=getattr(value, "open_price", None),
        high=getattr(value, "high", None),
        low=getattr(value, "low", None),
        volume=getattr(value, "volume", None),
    )


def _quote_for(
    ticker: str,
    quotes: Mapping[str, Any] | None,
    router: Any,
    stats: dict[str, int],
) -> MarketSnap | None:
    if quotes is not None:
        if ticker not in quotes:
            stats["fail"] = stats.get("fail", 0) + 1
            return None
        snap = _as_snap(quotes[ticker])
        if snap is None:
            stats["fail"] = stats.get("fail", 0) + 1
            return None
        _count_source(stats, snap.source)
        return snap
    if router is None:
        stats["fail"] = stats.get("fail", 0) + 1
        return None
    try:
        snap = _as_snap(router.fetch(ticker))
    except Exception:
        log.warning("quote failed ticker=%s", ticker)
        stats["fail"] = stats.get("fail", 0) + 1
        return None
    if snap is None:
        stats["fail"] = stats.get("fail", 0) + 1
        return None
    _count_source(stats, snap.source)
    return snap


def _count_source(stats: dict[str, int], source: str) -> None:
    if source == "yahoo":
        stats["yahoo"] = stats.get("yahoo", 0) + 1
    else:
        stats["ibkr"] = stats.get("ibkr", 0) + 1


def _fetch_quotes(config: MonitorConfig, tickers: list[str]) -> dict[str, MarketSnap]:
    """One Gateway read for the open book, then the public quote for gaps."""
    from auto_trading.config import get_settings
    from auto_trading.execution.ibkr import PaperGateway
    from auto_trading.market.quotes import yahoo_quote

    found: dict[str, MarketSnap] = {}
    rows: list[dict[str, Any]] = []
    try:
        import asyncio

        gateway = PaperGateway(get_settings(), client_id=config.client_id)
        rows = list(asyncio.run(gateway.snapshot_quotes(tickers)))
    except Exception:
        log.warning("gateway quotes unavailable")
    for row in rows:
        symbol = str(row.get("symbol") or "")
        snap = _snap_from_row(row)
        if symbol and snap is not None:
            found[symbol] = snap
    for ticker in tickers:
        if ticker in found:
            continue
        try:
            snap = _as_snap(yahoo_quote(ticker))
        except Exception:
            log.warning("public quote failed ticker=%s", ticker)
            snap = None
        if snap is not None:
            found[ticker] = snap
    return found


def _snap_from_row(row: dict[str, Any]) -> MarketSnap | None:
    try:
        last = float(row["last"])
    except (KeyError, TypeError, ValueError):
        return None
    delay = row.get("delay_sec")
    try:
        delay_sec = None if delay is None else float(delay)
    except (TypeError, ValueError):
        delay_sec = None
    return MarketSnap(last=last, source=str(row.get("source") or "ibkr"), delay_sec=delay_sec)


def _maybe_snapshot(
    config: MonitorConfig,
    loaded: PositionLoad,
    now: datetime,
    state: dict[str, Any],
) -> None:
    if loaded.source == "none":
        return
    nonempty = bool(_open_positions(loaded.positions))
    if not nonempty:
        age = snapshot_age_sec(loaded.snapshot_at or state.get("last_snapshot_at"), now)
        if loaded.source == "snapshot" and age is not None and age < EMPTY_SNAPSHOT_SEC:
            return
    write_holdings_snapshot(
        config.trading_home,
        loaded.positions,
        account=config.account,
        now=now,
        source_at=loaded.source_at,
        snapshot_path=config.snapshot_path,
    )
    state["last_snapshot_at"] = iso_macau(now)


def _data_error_due(state: dict[str, Any], now: datetime) -> bool:
    seen = parse_moment(state.get("data_error_at"))
    if seen is None:
        return True
    return (now - seen).total_seconds() >= DATA_ERROR_COOLDOWN_SEC


def _emit_drafts(
    config: MonitorConfig,
    drafts: list[tuple[AlertDraft, str, PositionView, MarketSnap]],
    state: dict[str, Any],
    now: datetime,
    trading_date: str,
) -> int:
    pending: list[tuple[AlertDraft, str, PositionView, MarketSnap]] = []
    active: set[tuple[str, str]] = set()
    for draft, key, position, snap in drafts:
        active.add((draft.ticker, key))
        bucket = _alert_bucket(state, draft.ticker)
        previous = bucket.get(key)
        if _should_emit(
            previous if isinstance(previous, dict) else None,
            draft.severity,
            now,
            warn_sec=config.warn_sec,
            crit_sec=config.crit_sec,
        ):
            pending.append((draft, key, position, snap))
        else:
            _mark(bucket, key, now, draft.severity, emitted=False)
    if len(pending) > MAX_ALERTS:
        _write_summary(config, pending, now, trading_date)
        for draft, key, _position, _snap in pending:
            _mark(_alert_bucket(state, draft.ticker), key, now, draft.severity, emitted=True)
        _clear_quiet(state, active)
        return 1
    for draft, key, position, snap in pending:
        _write_one(config, draft, position, snap, now, trading_date)
        _mark(_alert_bucket(state, draft.ticker), key, now, draft.severity, emitted=True)
    _clear_quiet(state, active)
    return len(pending)


def _write_summary(
    config: MonitorConfig,
    pending: list[tuple[AlertDraft, str, PositionView, MarketSnap]],
    now: datetime,
    trading_date: str,
) -> None:
    rules = sorted({item[0].rule_id for item in pending})
    alert = build_alert(
        rule_id="hm_summary",
        alert_type="holdings_summary",
        severity="warning",
        ticker="MULTI",
        message_zh=f"本輪持倉提醒 {len(pending)} 則，已彙總。",
        trading_date=trading_date,
        direction="na",
        created_at=as_macau(now),
        extra={
            "context": {
                "rules": rules,
                "count": len(pending),
                "account_status": account_status(config.trading_home),
            },
        },
    )
    write_hm_alert(config.trading_home, alert, filename_stamp=stamp(now))


def _write_one(
    config: MonitorConfig,
    draft: AlertDraft,
    position: PositionView,
    snap: MarketSnap,
    now: datetime,
    trading_date: str,
) -> None:
    delay_min = None if snap.delay_sec is None else snap.delay_sec / 60.0
    if position.strategy_id:
        file_ticker = f"{position.ticker}_{position.strategy_id}"
    else:
        file_ticker = position.ticker
    alert = build_alert(
        rule_id=draft.rule_id,
        alert_type=draft.alert_type,
        severity=draft.severity,
        ticker=position.ticker,
        message_zh=draft.message_zh,
        trading_date=trading_date,
        direction=draft.direction,
        created_at=as_macau(now),
        extra={
            "market": position.market,
            "currency": position.currency,
            "price": snap.last,
            "price_time": iso_macau(now),
            "price_source": snap.source,
            "data_delay_min": delay_min,
            "context": draft.context,
        },
    )
    key = _monitor_key(draft, trading_date, position.strategy_id)
    alert["dedup_key"] = key
    alert["alert_id"] = make_alert_id(key, trading_date)
    write_hm_alert(
        config.trading_home,
        alert,
        filename_stamp=stamp(now),
        file_ticker=file_ticker,
    )


def _write_data_error(config: MonitorConfig, now: datetime, *, retry: bool) -> None:
    trading_date = as_macau(now).date().isoformat()
    message = "持倉快照未完成，下一輪再讀。" if retry else "讀不到持倉快照或帳戶檔。"
    alert = build_alert(
        rule_id="hm_data_error",
        alert_type="holdings_data_error",
        severity="warning",
        ticker="MULTI",
        message_zh=message,
        trading_date=trading_date,
        direction="na",
        created_at=as_macau(now),
        extra={"context": {"account_status": account_status(config.trading_home), "retry": retry}},
    )
    write_hm_alert(config.trading_home, alert, filename_stamp=stamp(now))


def run_once(
    config: MonitorConfig,
    now: datetime,
    *,
    quotes: Mapping[str, Any] | None = None,
    bars: Mapping[str, BarContext] | None = None,
    quote_router: Any = None,
) -> dict[str, Any]:
    """One tick. Partial snapshots retry next time and do not raise."""
    moment = as_macau(now)
    loaded = load_positions(config.trading_home, config.snapshot_path)
    status = account_status(config.trading_home)
    state = load_state(config.trading_home, config.interval_sec)
    stats = {"ibkr": 0, "yahoo": 0, "fail": 0}
    positions = _open_positions(loaded.positions)
    state["last_tick_at"] = iso_macau(moment)
    state["interval_sec"] = config.interval_sec
    state["n_positions"] = len(positions)
    state["retry_incomplete_snapshot"] = loaded.retry_incomplete_snapshot
    state["account_status"] = status
    state["quote_stats"] = stats
    alerts_written = 0
    if loaded.source == "none" and _data_error_due(state, moment):
        _write_data_error(config, moment, retry=loaded.retry_incomplete_snapshot)
        state["data_error_at"] = iso_macau(moment)
        alerts_written += 1
    elif positions:
        use_quotes = quotes
        router = quote_router
        if use_quotes is None and router is None:
            use_quotes = _fetch_quotes(config, [item.ticker for item in positions])
            router = None
        orders = load_broker_stops(config.trading_home)
        drafts: list[tuple[AlertDraft, str, PositionView, MarketSnap]] = []
        trading_date = moment.date().isoformat()
        for position in positions:
            snap = _quote_for(position.ticker, use_quotes, router, stats)
            if snap is None:
                continue
            try:
                found = evaluate_position(
                    position,
                    snap,
                    _bars_for(config.trading_home, position.ticker, bars),
                    market_is_open=market_open(position.market, moment),
                    broker_stop=broker_stop_for(position, orders),
                    account_status=status,
                )
            except Exception:
                log.warning("check failed ticker=%s", position.ticker)
                stats["fail"] = stats.get("fail", 0) + 1
                continue
            for draft in found:
                if not market_open(position.market, moment) and draft.rule_id in SESSION_RULES:
                    continue
                severity = apply_status_severity(draft.severity, status)
                adjusted = AlertDraft(
                    rule_id=draft.rule_id,
                    alert_type=draft.alert_type,
                    severity=severity,
                    ticker=draft.ticker,
                    direction=draft.direction,
                    message_zh=draft.message_zh,
                    context=dict(draft.context),
                )
                key = _monitor_key(adjusted, trading_date, position.strategy_id)
                drafts.append((adjusted, key, position, snap))
        alerts_written += _emit_drafts(config, drafts, state, moment, trading_date)
    else:
        _clear_quiet(state, set())
    _maybe_snapshot(config, loaded, moment, state)
    if loaded.source != "none":
        state["last_ok_at"] = iso_macau(moment)
    state["quote_stats"] = stats
    save_state(config.trading_home, state)
    return {
        "n_positions": len(positions),
        "alerts": alerts_written,
        "retry_incomplete_snapshot": loaded.retry_incomplete_snapshot,
        "account_status": status,
        "quote_stats": stats,
        "position_source": loaded.source,
    }


def run_forever(
    config: MonitorConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
    quotes: Mapping[str, Any] | None = None,
    bars: Mapping[str, BarContext] | None = None,
    quote_router: Any = None,
    max_ticks: int | None = None,
) -> None:
    """Long-running loop. One failed tick is logged and the next tick still runs."""
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        started = time.monotonic()
        moment = clock() if clock is not None else datetime.now(MACAU)
        try:
            run_once(config, moment, quotes=quotes, bars=bars, quote_router=quote_router)
        except Exception:
            log.exception("holdings tick failed")
        elapsed = time.monotonic() - started
        pause, overrun = wait_seconds(elapsed, config.interval_sec)
        _set_overrun(config, overrun)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            return
        if pause > 0:
            sleep(pause)


def _set_overrun(config: MonitorConfig, overrun: bool) -> None:
    try:
        state = load_state(config.trading_home, config.interval_sec)
        state["tick_overrun"] = overrun
        save_state(config.trading_home, state)
    except OSError:
        log.warning("could not record tick_overrun")


def _write_startup_refusal(home: Path, code: str) -> None:
    now = datetime.now(MACAU)
    try:
        alert = build_alert(
            rule_id="hm_startup",
            alert_type="holdings_startup",
            severity="critical",
            ticker="ACCOUNT",
            message_zh=f"持倉監察拒絕啟動：{code}。",
            trading_date=as_macau(now).date().isoformat(),
            direction="na",
            created_at=as_macau(now),
            extra={"context": {"code": code, "account_status": "running"}},
        )
        write_hm_alert(home, alert, filename_stamp=stamp(now))
    except OSError:
        log.warning("startup refusal alert was not written")


def main(argv: list[str] | None = None) -> int:
    """``python -m auto_trading.alerts.holdings_monitor``."""
    parser = argparse.ArgumentParser(prog="holdings-monitor")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--home", default="")
    args = parser.parse_args(argv)
    home = Path(args.home) if args.home else None
    try:
        config = config_from_env(home)
    except HoldingsMonitorError as exc:
        target = home or Path(os.environ.get("TRADING_HOME", "/srv/trading"))
        _write_startup_refusal(target, exc.code)
        return 2
    if args.once:
        run_once(config, datetime.now(MACAU))
        return 0
    run_forever(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
