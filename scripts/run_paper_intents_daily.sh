#!/bin/sh
# 07:05 Asia/Macau paper session. One timer calls this script, which calls
# auto-trading run-session. Do not add another daily runner.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
HOME_DIR="${TRADING_HOME:-$ROOT}"
SESSION_DATE=$(TZ=Asia/Macau date +%F)
BIN="$ROOT/.venv/bin/auto-trading"
if [ ! -x "$BIN" ]; then
  BIN=auto-trading
fi
SIGNALS="${SIGNALS_PATH:-$HOME_DIR/signals.json}"
BARS="${BARS_CACHE:-$HOME_DIR/data/bars_cache}"
exec "$BIN" run-session \
  --date "$SESSION_DATE" \
  --trading-home "$HOME_DIR" \
  --signals "$SIGNALS" \
  --bars-cache "$BARS"
