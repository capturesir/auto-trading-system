"""Backfill the 2026-10-16 SPY paper fill into the local journal.

Default is a dry run. ``--apply`` copies the journal, then records the fill.
``--undo`` restores that copy when no later order or execution was added.

See ``docs/backfill_spy_20261016.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from auto_trading.execution.spy_backfill import (
    BackfillError,
    apply_backfill,
    inspect_plan,
    undo_backfill,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the 2026-10-16 SPY paper fill.")
    parser.add_argument(
        "--home",
        default=os.environ.get("TRADING_HOME", ""),
        help="Trading home (default: TRADING_HOME).",
    )
    parser.add_argument("--apply", action="store_true", help="Write the journal.")
    parser.add_argument("--undo", action="store_true", help="Restore the pre-apply backup.")
    args = parser.parse_args(argv)
    if args.apply and args.undo:
        print("pass only one of --apply or --undo", file=sys.stderr)
        return 1
    if not args.home:
        print("pass --home or set TRADING_HOME", file=sys.stderr)
        return 1
    home = Path(args.home)
    try:
        if args.undo:
            payload = undo_backfill(home)
        elif args.apply:
            payload = apply_backfill(home)
        else:
            payload = {"dry_run": True, "plan": inspect_plan(home)}
    except BackfillError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
