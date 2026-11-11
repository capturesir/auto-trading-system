#!/bin/sh
# Clock loop for the paper timers. Intervals match vps/deploy/*.timer.
# A missing stamp waits for the next slot. A stamp older than the previous
# slot runs once, then waits. This does not open a broker socket by itself.
set -eu

next_epoch_daily() {
  tz=$1
  hour=$2
  minute=$3
  now=$(date +%s)
  today=$(TZ="$tz" date +%F)
  slot=$(TZ="$tz" date -d "${today} ${hour}:${minute}:00" +%s)
  if [ "$slot" -le "$now" ]; then
    next_day=$(TZ="$tz" date -d "${today} + 1 day" +%F)
    slot=$(TZ="$tz" date -d "${next_day} ${hour}:${minute}:00" +%s)
  fi
  printf '%s\n' "$slot"
}

previous_epoch_daily() {
  tz=$1
  hour=$2
  minute=$3
  now=$(date +%s)
  today=$(TZ="$tz" date +%F)
  slot=$(TZ="$tz" date -d "${today} ${hour}:${minute}:00" +%s)
  if [ "$slot" -gt "$now" ]; then
    prev_day=$(TZ="$tz" date -d "${today} - 1 day" +%F)
    slot=$(TZ="$tz" date -d "${prev_day} ${hour}:${minute}:00" +%s)
  fi
  printf '%s\n' "$slot"
}

schedule_of() {
  case "$1" in
    strategy) printf '%s %s %s\n' UTC 23 05 ;;
    summary) printf '%s %s %s\n' Asia/Macau 06 00 ;;
    pool) printf '%s %s %s\n' Asia/Hong_Kong 06 00 ;;
    *) return 1 ;;
  esac
}

print_next() {
  case "$1" in
    sync) printf '30\n' ;;
    reeval) printf '120\n' ;;
    strategy|summary|pool)
      # shellcheck disable=SC2046
      next_epoch_daily $(schedule_of "$1")
      ;;
    *)
      echo "unknown timer: $1" >&2
      return 1
      ;;
  esac
}

if [ "${1:-}" = "--print-next" ]; then
  print_next "${2:?}"
  exit 0
fi

name=${1:?}
cd /srv/trading
home=${TRADING_HOME:-/srv/trading}
stamp_dir="$home/state/timers"
mkdir -p "$stamp_dir"
stamp="$stamp_dir/${name}.stamp"
py=/srv/trading/.venv/bin/python

run_job() {
  code=0
  case "$name" in
    strategy) /srv/trading/scripts/run_paper_intents_daily.sh || code=$? ;;
    summary) "$py" -m auto_trading.execution summary || code=$? ;;
    pool) /srv/trading/scripts/pool_rebuild.sh || code=$? ;;
    sync) "$py" -m auto_trading.execution sync || code=$? ;;
    reeval) "$py" -m auto_trading.intraday --once || code=$? ;;
    *)
      echo "unknown timer: $name" >&2
      return 1
      ;;
  esac
  echo "timer ${name} exit ${code}"
}

case "$name" in
  sync)
    sleep 30
    while true; do
      run_job
      sleep 30
    done
    ;;
  reeval)
    sleep 120
    while true; do
      run_job
      sleep 120
    done
    ;;
  strategy|summary|pool)
    while true; do
      # shellcheck disable=SC2046
      prev=$(previous_epoch_daily $(schedule_of "$name"))
      last=0
      if [ -f "$stamp" ]; then
        last=$(tr -cd '0-9' < "$stamp" || true)
        last=${last:-0}
      fi
      if [ "$last" -gt 0 ] && [ "$last" -lt "$prev" ]; then
        run_job
        date -u +%s > "$stamp"
      fi
      # shellcheck disable=SC2046
      slot=$(next_epoch_daily $(schedule_of "$name"))
      now=$(date +%s)
      delay=$((slot - now))
      if [ "$delay" -lt 1 ]; then
        delay=1
      fi
      echo "timer ${name} sleep ${delay}"
      sleep "$delay"
      run_job
      date -u +%s > "$stamp"
    done
    ;;
  *)
    echo "unknown timer: $name" >&2
    exit 1
    ;;
esac
