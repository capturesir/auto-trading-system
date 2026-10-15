"""One paper session. The daily timer calls ``run_paper_intents_daily.sh``.

This script is the manual one-shot for the same ``run_session`` function as
``auto-trading run-session``. It is not a second runner.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_paper_intents_once")
    parser.add_argument("--trading-home", type=Path, default=None)
    parser.add_argument("--signals", type=Path, default=None)
    parser.add_argument("--approved-dir", type=Path, default=None)
    parser.add_argument("--bars-cache", type=Path, default=None)
    parser.add_argument("--session-date", "--date", dest="session_date", required=True)
    entries = parser.add_mutually_exclusive_group()
    entries.add_argument("--entries-allowed", dest="entries_allowed", action="store_true")
    entries.add_argument("--no-entries-allowed", dest="entries_allowed", action="store_false")
    parser.set_defaults(entries_allowed=None)
    args = parser.parse_args(argv)
    from auto_trading.strategy.session import SessionError, run_session

    home = args.trading_home or Path("/srv/trading")
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
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
