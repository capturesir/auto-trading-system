"""Command line: print a strategy hash, or run a backtest from cached bars."""

from __future__ import annotations

import argparse
import json
import os
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
    trade = sub.add_parser("trade", help="paper operator commands")
    trade.add_argument("trade_args", nargs=argparse.REMAINDER)
    session = sub.add_parser("run-session", help="write paper intents for one session")
    session.add_argument("--date", required=True, dest="session_date")
    session.add_argument("--trading-home", type=Path, default=None)
    session.add_argument("--signals", type=Path, default=None)
    session.add_argument("--bars-cache", type=Path, default=None)
    session.add_argument("--approved-dir", type=Path, default=None)
    entries = session.add_mutually_exclusive_group()
    entries.add_argument("--entries-allowed", dest="entries_allowed", action="store_true")
    entries.add_argument("--no-entries-allowed", dest="entries_allowed", action="store_false")
    session.set_defaults(entries_allowed=None)
    args = parser.parse_args(argv)
    if args.command == "trade":
        from auto_trading.cli_trade import main as trade_main

        trade_main(args.trade_args)
        return
    if args.command == "run-session":
        from auto_trading.strategy.session import SessionError, run_session

        home = args.trading_home
        if home is None:
            home = Path(os.environ.get("TRADING_HOME", "/srv/trading"))
        try:
            report = run_session(
                trading_home=home,
                session_date=args.session_date,
                signals_path=args.signals,
                bars_cache=args.bars_cache,
                approved_dir=args.approved_dir,
                entries_allowed=args.entries_allowed,
            )
        except SessionError as exc:
            raise SystemExit(str(exc)) from exc
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
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
