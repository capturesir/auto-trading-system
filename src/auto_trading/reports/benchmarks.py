"""Public benchmark comparison for the daily summary and the monthly review.

Prices come from Yahoo, Stooq, Coinbase, Kraken, and CoinGecko. This module
does not call a broker. A missing, stale, or timed-out series is ``n/a`` and
does not stop the summary.
"""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from auto_trading.market.calendar import is_session

NEW_YORK = ZoneInfo("America/New_York")
MACAU = ZoneInfo("Asia/Macau")
REQUEST_TIMEOUT_SEC = 10.0
ROUND_TIMEOUT_SEC = 60.0
HOUR = 3600.0
REPO_CONFIG = Path(__file__).resolve().parents[3] / "config" / "benchmarks.json"
Getter = Callable[[str, float], str]


class BenchmarkFetchError(Exception):
    """One source failed. ``reason`` is a ``na_reason`` value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Point:
    day: str
    price: float
    price_basis: str
    source: str


@dataclass
class ManualClock:
    """Test clock. Production uses :class:`WallClock`."""

    now: float = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class WallClock:
    """Seconds since this round started, from a monotonic clock."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    @property
    def now(self) -> float:
        return time.monotonic() - self._started


def et_close(day: date) -> datetime:
    """16:00 America/New_York on ``day``. Macau is 04:00 in summer and 05:00 in winter."""
    return datetime(day.year, day.month, day.day, 16, 0, tzinfo=NEW_YORK)


def max_drawdown(prices: list[float]) -> float | None:
    """``min_t(P_t / max_{s<=t} P_s - 1)``. The result is at most zero."""
    if not prices:
        return None
    peak = prices[0]
    worst = 0.0
    for price in prices:
        if price > peak:
            peak = price
        if peak:
            worst = min(worst, price / peak - 1.0)
    return worst


def load_benchmarks(home: Path) -> dict[str, Any]:
    """Read the trading-home file, or the repository copy when it is absent."""
    path = home / "config" / "benchmarks.json"
    if not path.is_file():
        path = REPO_CONFIG
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != "benchmarks/1.0":
        raise BenchmarkFetchError("parse_error")
    return payload


def paper_start(home: Path) -> dict[str, Any] | None:
    """E2 start record. Missing means the comparison has not started."""
    path = home / "state" / "paper_start.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("paper_start_date"):
        return None
    return payload


def build_benchmarks(
    home: Path,
    now: datetime,
    account: dict[str, Any],
    *,
    get: Getter | None = None,
    clock: ManualClock | WallClock | None = None,
) -> dict[str, Any] | None:
    """Comparison block for one risk day, or None before E2 has started."""
    started = paper_start(home)
    if started is None:
        _event(home, now, None, "not_started")
        return None
    risk_date = _risk_date(now)
    try:
        catalog = load_benchmarks(home)
    except (OSError, json.JSONDecodeError, BenchmarkFetchError):
        _event(home, now, None, "parse_error")
        return _shell(started, risk_date, account, [])
    timer = clock or WallClock()
    deadline = timer.now + ROUND_TIMEOUT_SEC
    transport = get or _urllib_get
    items: list[dict[str, Any]] = []
    for item in catalog.get("items") or []:
        try:
            items.append(_one(home, item, started, risk_date, now, transport, timer, deadline))
        except (BenchmarkFetchError, OSError, ValueError):
            items.append(_na(home, now, str(item.get("id") or ""), "fetch_error", "close", None))
    block = _shell(started, risk_date, account, items)
    _store_paper(home, risk_date, account, now)
    apply_paper_drawdown(home, block)
    return block


def write_monthly_review(home: Path, month: str, *, now: datetime) -> dict[str, Any] | None:
    """Write ``reports/monthly/review_<YYYYMM>.json`` and the matching Markdown."""
    started = paper_start(home)
    if started is None:
        return None
    try:
        catalog = load_benchmarks(home)
    except (OSError, json.JSONDecodeError, BenchmarkFetchError):
        catalog = {"items": []}
    paper = _paper_stats(home, started, month)
    rows = []
    for item in catalog.get("items") or []:
        rows.append(_month_item(home, item, started, month, paper))
    payload = {
        "schema_version": "monthly_review/1.0",
        "month": month,
        "generated_at": now.astimezone(MACAU).isoformat(timespec="seconds"),
        "paper_start_date": started["paper_start_date"],
        "paper": paper,
        "benchmarks": rows,
    }
    folder = home / "reports" / "monthly"
    folder.mkdir(parents=True, exist_ok=True)
    stem = "review_" + month.replace("-", "")
    target = folder / f"{stem}.json"
    _atomic(target, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _atomic(folder / f"{stem}.md", _month_markdown(payload))
    return payload


def _shell(
    started: dict[str, Any],
    risk_date: date,
    account: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    paper = _paper_today(started, risk_date, account)
    for item in items:
        item["excess_day"] = _excess(paper["day_return"], item.get("day_return"))
        item["excess_since_start"] = _excess(
            paper["since_start_return"], item.get("since_start_return")
        )
    return {
        "paper_start_date": started["paper_start_date"],
        "us_trade_date": risk_date.isoformat(),
        "paper": paper,
        "items": items,
    }


def _one(
    home: Path,
    item: dict[str, Any],
    started: dict[str, Any],
    risk_date: date,
    now: datetime,
    get: Getter,
    clock: ManualClock | WallClock,
    deadline: float,
) -> dict[str, Any]:
    benchmark_id = str(item.get("id") or "")
    kind = str(item.get("kind") or "equity")
    basis = "snapshot_1600_ET" if kind == "crypto" else "adj_close"
    try:
        start_day = date.fromisoformat(str(started["paper_start_date"]))
    except ValueError:
        return _na(home, now, benchmark_id, "missing_base", basis, None)
    if clock.now >= deadline:
        return _na(home, now, benchmark_id, "fetch_timeout", basis, None)
    needed = _needed_days(kind, start_day, risk_date)
    stored = _read_series(home, benchmark_id)
    reasons: list[str] = []
    if any(day.isoformat() not in stored for day in needed):
        for source in item.get("sources") or []:
            if clock.now >= deadline:
                reasons.append("fetch_timeout")
                break
            name = str(source.get("name") or "")
            symbol = str(source.get("symbol") or "")
            try:
                body = _request(source_url(name, symbol), get, clock, deadline)
                fresh = _parse(name, kind, body)
            except BenchmarkFetchError as exc:
                reasons.append(exc.reason)
                continue
            except TimeoutError:
                reasons.append("fetch_timeout")
                continue
            if not fresh:
                reasons.append("parse_error")
                continue
            _append_series(home, benchmark_id, fresh, now)
            stored = _read_series(home, benchmark_id)
            break
        else:
            reason = _aggregate(reasons, clock, deadline)
            return _na(home, now, benchmark_id, reason, basis, None)
        if any(day.isoformat() not in stored for day in needed) and not stored:
            reason = _aggregate(reasons, clock, deadline)
            return _na(home, now, benchmark_id, reason, basis, None)
    return _classify(home, now, benchmark_id, kind, stored, start_day, risk_date)


def _aggregate(reasons: list[str], clock: ManualClock | WallClock, deadline: float) -> str:
    if clock.now >= deadline:
        return "fetch_timeout"
    if reasons and all(reason == "fetch_timeout" for reason in reasons):
        return "fetch_timeout"
    if reasons and all(reason == "parse_error" for reason in reasons):
        return "parse_error"
    return "fetch_error"


def _classify(
    home: Path,
    now: datetime,
    benchmark_id: str,
    kind: str,
    stored: dict[str, Point],
    start_day: date,
    risk_date: date,
) -> dict[str, Any]:
    basis = "snapshot_1600_ET" if kind == "crypto" else "adj_close"
    base_day = _base_day(kind, start_day)
    base = stored.get(base_day.isoformat())
    session = kind == "crypto" or is_session("XNYS", risk_date)
    if session:
        current = stored.get(risk_date.isoformat())
        if current is None:
            newest = _newest(stored, risk_date)
            reason = "stale" if newest is not None else "fetch_error"
            return _na(home, now, benchmark_id, reason, basis, newest)
        if base is None:
            return _na(home, now, benchmark_id, "missing_base", current.price_basis, current)
        previous = _previous_point(kind, stored, risk_date)
        prices = _path(kind, stored, base_day, risk_date)
        return _ok(
            benchmark_id,
            current,
            _ret(current.price, previous.price) if previous else None,
            _ret(current.price, base.price),
            max_drawdown(prices),
        )
    newest = _newest(stored, risk_date)
    if newest is None:
        return _na(home, now, benchmark_id, "fetch_error", basis, None)
    if base is None:
        return _na(home, now, benchmark_id, "missing_base", newest.price_basis, newest)
    prices = _path(kind, stored, base_day, risk_date)
    row = _ok(benchmark_id, newest, None, _ret(newest.price, base.price), max_drawdown(prices))
    row["status"] = "closed"
    row["na_reason"] = "us_market_closed"
    return row


def _ok(
    benchmark_id: str,
    point: Point,
    day_return: float | None,
    since: float | None,
    drawdown: float | None,
) -> dict[str, Any]:
    return {
        "id": benchmark_id,
        "status": "ok",
        "price_basis": point.price_basis,
        "source": point.source,
        "as_of": point.day,
        "day_return": day_return,
        "since_start_return": since,
        "max_drawdown": drawdown,
        "excess_day": None,
        "excess_since_start": None,
        "na_reason": None,
    }


def _na(
    home: Path,
    now: datetime,
    benchmark_id: str | None,
    reason: str,
    basis: str,
    point: Point | None,
) -> dict[str, Any]:
    _event(home, now, benchmark_id, reason)
    return {
        "id": benchmark_id,
        "status": "n/a",
        "price_basis": point.price_basis if point else basis,
        "source": point.source if point else None,
        "as_of": point.day if point else None,
        "day_return": None,
        "since_start_return": None,
        "max_drawdown": None,
        "excess_day": None,
        "excess_since_start": None,
        "na_reason": reason,
    }


def _paper_today(
    started: dict[str, Any], risk_date: date, account: dict[str, Any]
) -> dict[str, Any]:
    nlv = _number(account.get("nlv"))
    day_start = _number(account.get("day_start_nlv"))
    if day_start is None:
        day_start = _number(account.get("risk_day_start_nlv"))
    origin = _number(started.get("start_nlv"))
    day_return = _ret(nlv, day_start) if nlv is not None and day_start else None
    since = _ret(nlv, origin) if nlv is not None and origin else None
    return {
        "day_return": day_return,
        "since_start_return": since,
        "max_drawdown": None,
    }


def _store_paper(home: Path, risk_date: date, account: dict[str, Any], now: datetime) -> None:
    nlv = _number(account.get("nlv"))
    if nlv is None:
        return
    _append_series(
        home,
        "paper",
        [Point(risk_date.isoformat(), nlv, "nlv", "paper")],
        now,
    )
    started = paper_start(home)
    if started is None:
        return
    origin = _number(started.get("start_nlv"))
    series = _read_series(home, "paper")
    prices: list[float] = []
    if origin is not None:
        prices.append(origin)
    for day in sorted(series):
        if day <= risk_date.isoformat():
            prices.append(series[day].price)
    block_path = home / "data" / "benchmarks" / "paper_drawdown.json"
    # The caller fills max_drawdown from this file after the items are built.
    block_path.parent.mkdir(parents=True, exist_ok=True)
    block_path.write_text(
        json.dumps({"max_drawdown": max_drawdown(prices)}),
        encoding="utf-8",
    )


def apply_paper_drawdown(home: Path, block: dict[str, Any] | None) -> None:
    """Copy the stored paper drawdown onto the block. Missing data stays null."""
    if block is None:
        return
    path = home / "data" / "benchmarks" / "paper_drawdown.json"
    if not path.is_file():
        return
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    paper = block.get("paper")
    if isinstance(paper, dict):
        paper["max_drawdown"] = saved.get("max_drawdown")


def _paper_stats(home: Path, started: dict[str, Any], month: str) -> dict[str, Any]:
    origin = _number(started.get("start_nlv"))
    series = _read_series(home, "paper")
    end = _month_end(month)
    prices = [origin] if origin is not None else []
    in_month = [series[day].price for day in sorted(series) if day.startswith(month)]
    through = [series[day].price for day in sorted(series) if day <= end.isoformat()]
    prices.extend(through)
    last = in_month[-1] if in_month else None
    previous = _previous_month_last(series, month)
    base = previous if previous is not None else origin
    month_return = _ret(last, base) if last is not None and base else None
    since = _ret(last, origin) if last is not None and origin else None
    return {
        "month_return": month_return,
        "since_start_return": since,
        "max_drawdown": max_drawdown(prices),
        "excess_month": None,
        "excess_since_start": None,
    }


def _month_item(
    home: Path,
    item: dict[str, Any],
    started: dict[str, Any],
    month: str,
    paper: dict[str, Any],
) -> dict[str, Any]:
    benchmark_id = str(item.get("id") or "")
    kind = str(item.get("kind") or "equity")
    try:
        start_day = date.fromisoformat(str(started["paper_start_date"]))
    except ValueError:
        return _month_na(benchmark_id, "missing_base", paper)
    stored = _read_series(home, benchmark_id)
    end = _month_end(month)
    base_day = _base_day(kind, start_day)
    base = stored.get(base_day.isoformat())
    in_month = [
        stored[day] for day in sorted(stored) if day.startswith(month) and day <= end.isoformat()
    ]
    if not in_month or base is None:
        return _month_na(benchmark_id, "missing_base", paper)
    last = in_month[-1]
    previous = _previous_month_point(stored, month)
    denominator = previous.price if previous is not None else base.price
    prices = _path(kind, stored, base_day, end)
    month_return = _ret(last.price, denominator)
    since = _ret(last.price, base.price)
    return {
        "id": benchmark_id,
        "status": "ok",
        "month_return": month_return,
        "since_start_return": since,
        "max_drawdown": max_drawdown(prices),
        "excess_month": _excess(paper.get("month_return"), month_return),
        "excess_since_start": _excess(paper.get("since_start_return"), since),
        "na_reason": None,
    }


def _month_na(benchmark_id: str, reason: str, paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": benchmark_id,
        "status": "n/a",
        "month_return": None,
        "since_start_return": None,
        "max_drawdown": None,
        "excess_month": None,
        "excess_since_start": None,
        "na_reason": reason,
    }


def _month_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# 月度檢討 {payload['month']}",
        "",
        f"- 模擬盤當月 {payload['paper'].get('month_return')}",
        "",
        "| 基準 | 狀態 | 當月 | 開始至今 | 最大回撤 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in payload["benchmarks"]:
        lines.append(
            f"| {row['id']} | {row['status']} | {row['month_return']} | "
            f"{row['since_start_return']} | {row['max_drawdown']} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def source_url(name: str, symbol: str) -> str:
    """Public URL for one configured source. No broker host is used."""
    if name == "yahoo":
        return (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?interval=1d&events=div%2Csplit&includeAdjustedClose=true"
        )
    if name == "stooq":
        return f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    if name == "coinbase":
        return f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity=3600"
    if name == "kraken":
        return f"https://api.kraken.com/0/public/OHLC?pair={symbol}&interval=60"
    if name == "coingecko":
        return (
            "https://api.coingecko.com/api/v3/coins/"
            f"{symbol}/market_chart?vs_currency=usd&days=max&interval=hourly"
        )
    raise BenchmarkFetchError("fetch_error")


def _parse(name: str, kind: str, body: str) -> list[Point]:
    basis = "adj_close" if name == "yahoo" else "close"
    if kind == "crypto":
        basis = "snapshot_1600_ET"
    if name == "yahoo":
        return _parse_yahoo(body, name)
    if name == "stooq":
        return _parse_stooq(body, name)
    if name == "coinbase":
        return _parse_candles(body, name, basis, start_index=0, close_index=4, millis=False)
    if name == "kraken":
        return _parse_kraken(body, name, basis)
    if name == "coingecko":
        return _parse_coingecko(body, name, basis)
    raise BenchmarkFetchError("parse_error")


def _parse_yahoo(body: str, source: str) -> list[Point]:
    try:
        payload = json.loads(body)
        result = (payload.get("chart") or {}).get("result") or []
        block = result[0]
        stamps = block.get("timestamp") or []
        adjusted = ((block.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    except (json.JSONDecodeError, IndexError, AttributeError, TypeError) as exc:
        raise BenchmarkFetchError("parse_error") from exc
    if not isinstance(adjusted, list):
        raise BenchmarkFetchError("parse_error")
    points: list[Point] = []
    for index, stamp in enumerate(stamps):
        if index >= len(adjusted) or adjusted[index] is None:
            continue
        day = datetime.fromtimestamp(int(stamp), tz=NEW_YORK).date().isoformat()
        points.append(Point(day, float(adjusted[index]), "adj_close", source))
    return points


def _parse_stooq(body: str, source: str) -> list[Point]:
    points: list[Point] = []
    for row in csv.reader(body.splitlines()):
        if len(row) < 5 or row[0].lower() == "date":
            continue
        try:
            price = float(row[4])
            date.fromisoformat(row[0])
        except ValueError:
            continue
        points.append(Point(row[0], price, "close", source))
    return points


def _parse_candles(
    body: str,
    source: str,
    basis: str,
    *,
    start_index: int,
    close_index: int,
    millis: bool,
) -> list[Point]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise BenchmarkFetchError("parse_error") from exc
    if not isinstance(payload, list):
        raise BenchmarkFetchError("parse_error")
    raw: list[tuple[float, float]] = []
    for row in payload:
        if not isinstance(row, list) or len(row) <= close_index:
            continue
        try:
            start = float(row[start_index])
            close = float(row[close_index])
        except (TypeError, ValueError):
            continue
        if millis:
            start = start / 1000.0
        raw.append((start + HOUR, close))
    return _assign_crypto(raw, source, basis)


def _parse_kraken(body: str, source: str, basis: str) -> list[Point]:
    try:
        payload = json.loads(body)
        result = payload.get("result") or {}
    except (json.JSONDecodeError, AttributeError) as exc:
        raise BenchmarkFetchError("parse_error") from exc
    rows = None
    if isinstance(result, dict):
        for key, value in result.items():
            if key != "last" and isinstance(value, list):
                rows = value
                break
    if rows is None:
        raise BenchmarkFetchError("parse_error")
    return _parse_candles(
        json.dumps(rows),
        source,
        basis,
        start_index=0,
        close_index=4,
        millis=False,
    )


def _parse_coingecko(body: str, source: str, basis: str) -> list[Point]:
    try:
        payload = json.loads(body)
        prices = payload.get("prices")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise BenchmarkFetchError("parse_error") from exc
    if not isinstance(prices, list):
        raise BenchmarkFetchError("parse_error")
    raw: list[tuple[float, float]] = []
    for row in prices:
        if not isinstance(row, list) or len(row) < 2:
            continue
        try:
            raw.append((float(row[0]) / 1000.0, float(row[1])))
        except (TypeError, ValueError):
            continue
    return _assign_crypto(raw, source, basis)


def _assign_crypto(points: list[tuple[float, float]], source: str, basis: str) -> list[Point]:
    if not points:
        return []
    first = datetime.fromtimestamp(min(item[0] for item in points), tz=NEW_YORK).date()
    last = datetime.fromtimestamp(max(item[0] for item in points), tz=NEW_YORK).date()
    found: list[Point] = []
    day = first
    while day <= last:
        price = _closest(points, day)
        if price is not None:
            found.append(Point(day.isoformat(), price, basis, source))
        day += timedelta(days=1)
    return found


def _closest(points: list[tuple[float, float]], day: date) -> float | None:
    target = et_close(day).timestamp()
    best: float | None = None
    gap = HOUR + 1
    for stamp, price in points:
        distance = abs(stamp - target)
        if distance <= HOUR and distance < gap:
            best = price
            gap = distance
    return best


def _request(url: str, get: Getter, clock: ManualClock | WallClock, deadline: float) -> str:
    remaining = deadline - clock.now
    if remaining <= 0:
        raise BenchmarkFetchError("fetch_timeout")
    timeout = min(REQUEST_TIMEOUT_SEC, remaining)
    try:
        return get(url, timeout)
    except TimeoutError as exc:
        raise BenchmarkFetchError("fetch_timeout") from exc


def _urllib_get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _series_path(home: Path, benchmark_id: str) -> Path:
    return home / "data" / "benchmarks" / f"{benchmark_id}.csv"


def _read_series(home: Path, benchmark_id: str) -> dict[str, Point]:
    path = _series_path(home, benchmark_id)
    if not path.is_file():
        return {}
    found: dict[str, Point] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row.get("date"):
                continue
            found[row["date"]] = Point(
                row["date"],
                float(row["price"]),
                row.get("price_basis") or "",
                row.get("source") or "",
            )
    return found


def _append_series(home: Path, benchmark_id: str, points: list[Point], now: datetime) -> None:
    path = _series_path(home, benchmark_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_series(home, benchmark_id)
    seen = set(existing)
    fresh: list[Point] = []
    for point in points:
        if point.day in seen:
            continue
        seen.add(point.day)
        fresh.append(point)
    if not fresh:
        return
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    new_file = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["date", "price", "price_basis", "source", "fetched_at_utc"])
        for point in fresh:
            writer.writerow([point.day, point.price, point.price_basis, point.source, stamp])


def _needed_days(kind: str, start_day: date, risk_date: date) -> list[date]:
    base = _base_day(kind, start_day)
    days: list[date] = []
    day = base
    while day <= risk_date:
        if kind == "crypto" or is_session("XNYS", day):
            days.append(day)
        day += timedelta(days=1)
    return days


def _base_day(kind: str, start_day: date) -> date:
    if kind == "crypto":
        return start_day - timedelta(days=1)
    return _previous_session(start_day)


def _previous_session(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_session("XNYS", cursor):
        cursor -= timedelta(days=1)
    return cursor


def _previous_point(kind: str, stored: dict[str, Point], risk_date: date) -> Point | None:
    if kind == "crypto":
        return stored.get((risk_date - timedelta(days=1)).isoformat())
    return stored.get(_previous_session(risk_date).isoformat())


def _newest(stored: dict[str, Point], risk_date: date) -> Point | None:
    days = [day for day in stored if day <= risk_date.isoformat()]
    if not days:
        return None
    return stored[max(days)]


def _path(kind: str, stored: dict[str, Point], base: date, end: date) -> list[float]:
    prices: list[float] = []
    day = base
    while day <= end:
        if kind == "crypto" or is_session("XNYS", day):
            point = stored.get(day.isoformat())
            if point is not None:
                prices.append(point.price)
        day += timedelta(days=1)
    return prices


def _previous_month_last(stored: dict[str, Point], month: str) -> float | None:
    point = _previous_month_point(stored, month)
    return None if point is None else point.price


def _previous_month_point(stored: dict[str, Point], month: str) -> Point | None:
    year, month_number = (int(part) for part in month.split("-"))
    first = date(year, month_number, 1)
    end = (first - timedelta(days=1)).isoformat()
    days = [day for day in stored if day <= end]
    if not days:
        return None
    return stored[max(days)]


def _month_end(month: str) -> date:
    year, month_number = (int(part) for part in month.split("-"))
    if month_number == 12:
        return date(year + 1, 1, 1) - timedelta(days=1)
    return date(year, month_number + 1, 1) - timedelta(days=1)


def _risk_date(now: datetime) -> date:
    local = now.astimezone(MACAU)
    start = local.replace(hour=8, minute=0, second=0, microsecond=0)
    if local < start:
        start -= timedelta(days=1)
    return start.date()


def _ret(latest: float | None, base: float | None) -> float | None:
    if latest is None or base is None or base == 0:
        return None
    return latest / base - 1.0


def _excess(paper: float | None, bench: float | None) -> float | None:
    if paper is None or bench is None:
        return None
    return paper - bench


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _event(home: Path, now: datetime, benchmark_id: str | None, reason: str) -> None:
    path = home / "logs" / f"events_{now.astimezone(MACAU).strftime('%Y%m%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "event": "benchmark_na",
            "level": "info",
            "id": benchmark_id,
            "na_reason": reason,
        },
        ensure_ascii=False,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
