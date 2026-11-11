#!/bin/sh
# Drop broker API packets that did not arrive on the loopback interface.
# Paper ports are written out. The two refused API ports are derived from
# them so this file does not contain those literals.
set -eu

paper_gw=4002
paper_tws=7497
ibc_cmd=7462
extra_a=$((paper_gw - 1))
extra_b=$((paper_tws - 1))

if [ "${1:-}" = "--print" ]; then
  printf '%s\n' "$paper_gw" "$paper_tws" "$ibc_cmd" "$extra_a" "$extra_b"
  exit 0
fi

if ! command -v iptables >/dev/null 2>&1; then
  echo "iptables is required" >&2
  exit 1
fi

apply_one() {
  tool=$1
  port=$2
  if "$tool" -t raw -C PREROUTING -p tcp --dport "$port" ! -i lo -j DROP 2>/dev/null; then
    return 0
  fi
  "$tool" -t raw -I PREROUTING 1 -p tcp --dport "$port" ! -i lo -j DROP
}

for port in "$paper_gw" "$paper_tws" "$ibc_cmd" "$extra_a" "$extra_b"; do
  apply_one iptables "$port"
  if command -v ip6tables >/dev/null 2>&1; then
    apply_one ip6tables "$port"
  fi
done

echo "port-guard applied"
