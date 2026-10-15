"""Write the backtest directory the report spec names, minus chart images.

``metrics.json`` carries the §6.1 keys. Equity, trades, orders, and monthly
returns are CSV. HTML is a table of those metrics. PNG charts are not drawn.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from auto_trading.backtest.engine import BacktestResult


def write_report(dest: Path, result: BacktestResult, strategy: dict[str, Any]) -> Path:
    """Create the run directory and return it."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "strategy.json").write_text(
        json.dumps(strategy, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (dest / "metrics.json").write_text(
        json.dumps(result.metrics, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    _csv(
        dest / "equity_curve.csv",
        ["date", "equity"],
        [{"date": day, "equity": value} for day, value in result.equity],
    )
    _csv(dest / "trades.csv", _keys(result.trades), result.trades)
    _csv(dest / "orders.csv", _keys(result.orders), result.orders)
    monthly = result.metrics.get("monthly_returns") or []
    _csv(dest / "monthly_returns.csv", _keys(monthly), monthly)
    lines = ["<html><body><table>"]
    for key, value in result.metrics.items():
        lines.append(f"<tr><td>{key}</td><td>{value}</td></tr>")
    lines.append("</table></body></html>\n")
    (dest / "report.html").write_text("".join(lines), encoding="utf-8")
    (dest / "run.log").write_text("backtest report written\n", encoding="utf-8")
    return dest


def _keys(rows: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for row in rows:
        for key in row:
            if key not in found:
                found.append(key)
    return found


def _csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["empty"])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in (fields or ["empty"])})
