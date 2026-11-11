#!/bin/sh
# Forward one host command. The paper watchdog is the only caller.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "refusing: host systemctl wrapper is only for root" >&2
  exit 1
fi
if [ "$#" -ne 2 ] || [ "$1" != "restart" ] || [ "$2" != "ibgateway.service" ]; then
  echo "refusing: only restart ibgateway.service" >&2
  exit 1
fi
exec nsenter -t 1 -m -u -i -n -- /usr/bin/systemctl restart ibgateway.service
