#!/bin/sh
# Paper Gateway start. Guard keys are checked by render_template.py.
# ReadOnlyApi stays yes until E2 is approved by the user.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
CONFIG=${1:?usage: ibgw-start.sh <rendered.ini>}
python3 "$ROOT/vps/deploy/render_template.py" --check "$CONFIG"
