"""Schedule and run the monthly pool rebuild.

The systemd timer starts the shell script every day at 06:00 Hong Kong
time. This module decides whether today is the first XHKG session of the
month, or a later session that still owes a retry.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from auto_trading.alerts.critical import write_critical_event, write_ops_notice
from auto_trading.market.calendar import is_session
from auto_trading.universe.publish import RebuildError, publish_pool

log = logging.getLogger(__name__)
HKT = ZoneInfo("Asia/Hong_Kong")
TIMEOUT_SEC = 45 * 60
Runner = Callable[[str, list[str], float], int]


@dataclass
class RebuildOutcome:
    """Result of one timer firing."""

    ran: bool
    ok: bool
    step: str | None
    log_path: Path | None


def first_xhkg_business_day(year: int, month: int) -> date:
    """First XHKG session in that calendar month."""
    day = date(year, month, 1)
    for _ in range(31):
        if is_session("XHKG", day):
            return day
        day += timedelta(days=1)
    raise RuntimeError(f"no XHKG session in {year}-{month:02d}")


def should_run(today: date, *, retry_pending: bool) -> bool:
    """True on the month's first XHKG session, or on a retry session."""
    if retry_pending:
        return is_session("XHKG", today)
    return today == first_xhkg_business_day(today.year, today.month)


def retry_pending(home: Path) -> bool:
    """Whether the last rebuild is still waiting for the next session."""
    path = home / "state" / "pool_rebuild.json"
    if not path.is_file():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    return bool(payload.get("retry_pending"))


def set_retry(home: Path, pending: bool, *, step: str | None = None) -> None:
    """Record or clear the retry flag."""
    path = home / "state" / "pool_rebuild.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"retry_pending": pending, "step": step}
    path.write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")


def run_rebuild(
    home: Path,
    *,
    today: date,
    now: datetime,
    timeout_sec: float = TIMEOUT_SEC,
    steps: list[tuple[str, list[str]]] | None = None,
    runner: Runner | None = None,
    repo_root: Path | None = None,
) -> RebuildOutcome:
    """Run fetch, build, verify, then publish. Failures keep the last archive."""
    if not should_run(today, retry_pending=retry_pending(home)):
        return RebuildOutcome(False, True, None, None)
    run_id = now.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    universe = home / "data" / "universe"
    staging = universe / ".staging" / run_id
    staging.mkdir(parents=True, exist_ok=True)
    log_path = home / "logs" / f"pool_rebuild_{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_sec
    root = repo_root or Path(__file__).resolve().parents[3]
    planned = steps if steps is not None else _default_steps(root, staging)
    try:
        for name, argv in planned:
            _run_step(name, argv, deadline, runner, log_path)
        publish_pool(universe, staging, today=today)
    except Exception as exc:
        step = _failure_step(exc)
        _append(log_path, f"failed {step}: {exc}")
        _fail(home, step, log_path, now)
        return RebuildOutcome(True, False, step, log_path)
    else:
        set_retry(home, False)
        _append(log_path, "ok")
        return RebuildOutcome(True, True, None, log_path)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        parent = staging.parent
        if parent.name == ".staging" and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def check_pool_age(home: Path, today: date, now: datetime) -> str | None:
    """Warn from day 33 through day 40. Past that, send one critical alert."""
    meta_path = home / "data" / "universe" / "ta_pool.meta.json"
    if not meta_path.is_file():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    asof = date.fromisoformat(str(meta["asof"]))
    age = (today - asof).days
    if age > 40:
        _stale_once(home, asof, now)
        return "pool_stale"
    if age >= 33:
        _warn_once(home, today, asof, age, now)
        return "pool_age_warn"
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point for the timer. ``argv`` is accepted and unused."""
    del argv
    home = Path(os.environ.get("TRADING_HOME", Path.cwd()))
    now = datetime.now(HKT)
    outcome = run_rebuild(home, today=now.date(), now=now)
    check_pool_age(home, now.date(), now)
    return 0 if outcome.ok else 1


def _failure_step(exc: Exception) -> str:
    if isinstance(exc, RebuildError):
        return exc.step
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    return "error"


def _default_steps(root: Path, staging: Path) -> list[tuple[str, list[str]]]:
    folder = root / "research" / "universe"
    py = sys.executable
    return [
        ("fetch_sources", [py, str(folder / "fetch_sources.py")]),
        ("build_pool", [py, str(folder / "build_pool.py"), "--out", str(staging)]),
        (
            "verify_pool",
            [py, str(folder / "verify_pool.py"), "--root", str(staging)],
        ),
    ]


def _run_step(
    name: str,
    argv: list[str],
    deadline: float,
    runner: Runner | None,
    log_path: Path,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RebuildError("timeout")
    _append(log_path, "step " + name)
    try:
        if runner is not None:
            code = runner(name, argv, remaining)
        else:
            completed = subprocess.run(
                argv,
                check=False,
                timeout=remaining,
                capture_output=True,
            )
            _append(log_path, completed.stdout.decode("utf-8", errors="replace"))
            _append(log_path, completed.stderr.decode("utf-8", errors="replace"))
            code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        raise RebuildError("timeout") from exc
    if code != 0:
        raise RebuildError(name)


def _fail(home: Path, step: str, log_path: Path, now: datetime) -> None:
    set_retry(home, True, step=step)
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    write_critical_event(
        home,
        kind="pool_rebuild_failed",
        reason_code="POOL_REBUILD_FAILED",
        rule_id="POOL",
        now=moment,
        detail={"step": step, "log_path": str(log_path)},
        message_zh="股票池重建失敗，沿用上一份存檔",
        resume_hint=(
            "下一個香港交易所營業日 06:00 自動重試，或在主機本地重跑 scripts/pool_rebuild.sh"
        ),
    )
    try:
        from auto_trading.alerts.forwarder import scan_once

        scan_once(home, moment)
    except OSError:
        log.exception("pool rebuild alert was not forwarded")


def _warn_once(home: Path, today: date, asof: date, age: int, now: datetime) -> None:
    state = _age_state(home)
    stamp = today.isoformat()
    warned = list(state.get("warn_dates") or [])
    if stamp in warned:
        return
    write_ops_notice(
        home,
        kind="pool_age_warn",
        now=now,
        message_zh=f"股票池 asof {asof.isoformat()} 已 {age} 日",
    )
    warned.append(stamp)
    state["warn_dates"] = warned
    _write_age_state(home, state)


def _stale_once(home: Path, asof: date, now: datetime) -> None:
    state = _age_state(home)
    if state.get("stale_asof") == asof.isoformat():
        return
    moment = now if now.tzinfo else now.replace(tzinfo=UTC)
    write_critical_event(
        home,
        kind="pool_stale",
        reason_code="POOL_STALE",
        rule_id="POOL",
        now=moment,
        detail={"asof": asof.isoformat()},
        message_zh="股票池 asof 已超過 40 日，market 進場暫停",
        resume_hint="重建成功後恢復進場；出場和止損不受影響",
    )
    try:
        from auto_trading.alerts.forwarder import scan_once

        scan_once(home, moment)
    except OSError:
        log.exception("pool stale alert was not forwarded")
    state["stale_asof"] = asof.isoformat()
    _write_age_state(home, state)


def _age_state(home: Path) -> dict[str, object]:
    path = home / "state" / "pool_age.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_age_state(home: Path, payload: dict[str, object]) -> None:
    path = home / "state" / "pool_age.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")
