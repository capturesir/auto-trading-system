"""Command line: print a strategy hash, or run a backtest from cached bars."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from auto_trading.jsonutil import loads_strict
from auto_trading.strategy.spec import spec_hash


def _hash(path: Path) -> str:
    document = loads_strict(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{path} is not a JSON object")
    return spec_hash(document)


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``auto-trading``."""
    parser = argparse.ArgumentParser(prog="auto-trading")
    sub = parser.add_subparsers(dest="command", required=True)
    hashing = sub.add_parser("hash", help="print the spec hash of a strategy file")
    hashing.add_argument("path", type=Path)
    backtest = sub.add_parser("backtest", help="run a strategy against a JSON bar file")
    backtest.add_argument("strategy", type=Path)
    backtest.add_argument("--bars", type=Path, required=True)
    backtest.add_argument("--start", default="")
    backtest.add_argument("--end", default="")
    args = parser.parse_args(argv)
    if args.command == "hash":
        print(_hash(args.path))
        return
    from auto_trading.backtest.engine import Bar, BtSignal, run_backtest

    strategy = loads_strict(args.strategy.read_text(encoding="utf-8"))
    payload = json.loads(args.bars.read_text(encoding="utf-8"))
    bars = {
        ticker: [Bar(**row) for row in rows] for ticker, rows in payload.get("bars", {}).items()
    }
    signals = [BtSignal(**row) for row in payload.get("signals", [])]
    if args.start or args.end:
        bars = {
            ticker: [
                row
                for row in rows
                if (not args.start or row.date >= args.start)
                and (not args.end or row.date <= args.end)
            ]
            for ticker, rows in bars.items()
        }
    result = run_backtest(strategy, bars, signals)
    json.dump(result.metrics, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
