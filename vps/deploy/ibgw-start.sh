#!/usr/bin/env bash
# Started by ibgateway.service as user ibgw. PAPER ONLY. Contains no secrets.
# 1) render the IBC config from the template + systemd credentials into $RUNTIME_DIRECTORY (tmpfs, 0600)
# 2) start a private Xvfb on :99 (no TCP)
# 3) run IBC in Gateway paper mode
set -euo pipefail
set +x
umask 077
TEMPLATE=/etc/ibc/config.paper.ini.template
RUN_DIR="${RUNTIME_DIRECTORY:-/run/ibgateway}"
INI="$RUN_DIR/config.ini"
JTS=/var/lib/ibgw/Jts
LOG_DIR=/var/lib/ibgw/logs
XDISP=:99
TWS_MAJOR_VRSN=1050

: "${CREDENTIALS_DIRECTORY:?no systemd credentials; store them first with: sudo /usr/local/sbin/ibgw-set-credentials}"
for c in ibkr-paper-username ibkr-paper-password; do
  [[ -s "$CREDENTIALS_DIRECTORY/$c" ]] || { echo "missing credential $c" >&2; exit 1; }
done

python3 - "$TEMPLATE" "$INI" <<'PY'
import os, re, sys
tpl, dst = sys.argv[1], sys.argv[2]
text = open(tpl, encoding="utf-8").read()
def val(k):
    m = re.search(rf"^{k}=(.*)$", text, re.M)
    return m.group(1).strip() if m else None
required = {"TradingMode": "paper", "ReadOnlyApi": "yes", "OverrideTwsApiPort": "4002",
            "AcceptIncomingConnectionAction": "reject", "ExistingSessionDetectedAction": "secondary",
            "ExitAfterSecondFactorAuthenticationTimeout": "yes", "FIX": "no",
            "AutoRestartTime": "11:30 AM", "ColdRestartTime": "10:30"}
bad = [k for k, v in required.items() if val(k) != v]
if bad:
    sys.exit(f"refusing to start: template guard settings changed: {bad}")
cd = os.environ["CREDENTIALS_DIRECTORY"]
user = open(os.path.join(cd, "ibkr-paper-username"), encoding="utf-8").read().strip()
pw = open(os.path.join(cd, "ibkr-paper-password"), encoding="utf-8").read().rstrip("\r\n")
if not user or not pw or "\n" in user or "\n" in pw:
    sys.exit("refusing to start: empty or multi-line credential")
out = text.replace("__IBKR_PAPER_USERNAME__", user).replace("__IBKR_PAPER_PASSWORD__", pw)
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(out)
print("rendered IBC config into runtime dir (credentials not shown)")
PY

if [[ "${IBGW_RENDER_ONLY:-0}" == "1" ]]; then
  # dry check used by tests / the user: confirm the rendered file is complete, then delete it
  grep -q "__IBKR_PAPER_" "$INI" && { echo "placeholders left in rendered config" >&2; rm -f "$INI"; exit 1; }
  grep -E "^(TradingMode|ReadOnlyApi|OverrideTwsApiPort)=" "$INI"
  stat -c "rendered %n mode %a owner %U" "$INI"
  rm -f "$INI"; echo "render-only check OK (file removed; Gateway not started)"; exit 0
fi

mkdir -p "$LOG_DIR"
Xvfb "$XDISP" -screen 0 1280x1024x24 -nolisten tcp -nolisten local >>"$LOG_DIR/xvfb.log" 2>&1 &
for i in $(seq 1 20); do [[ -S /tmp/.X11-unix/X99 ]] && break; sleep 0.5; done
[[ -S /tmp/.X11-unix/X99 ]] || { echo "Xvfb did not start" >&2; exit 1; }

export DISPLAY="$XDISP" LOG_PATH="$LOG_DIR"
exec /opt/ibc/scripts/ibcstart.sh "$TWS_MAJOR_VRSN" --gateway --mode=paper \
  --tws-path="$JTS" --tws-settings-path="$JTS" --ibc-path=/opt/ibc --ibc-ini="$INI" --java-path=/opt/ibgateway/jre/bin \
  --on2fatimeout=exit
