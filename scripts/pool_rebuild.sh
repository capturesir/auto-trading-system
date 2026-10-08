#!/bin/sh
# Daily 06:00 HKT entry. The module runs only on the first XHKG session
# of the month, or when a retry is pending.
# VPS venv packages: pandas pyarrow lxml openpyxl yfinance exchange_calendars
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY=python3
fi
export TRADING_HOME="${TRADING_HOME:-$ROOT}"
exec "$PY" -m auto_trading.universe.rebuild
