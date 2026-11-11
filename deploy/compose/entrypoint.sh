#!/bin/sh
# Create the host uid inside the container, then run the service as that user.
set -eu
if [ "$#" -eq 0 ]; then
  echo "missing command" >&2
  exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
  exec "$@"
fi
uid=${ATS_UID:-1000}
gid=${ATS_GID:-1000}
if [ "$uid" = "0" ] || [ "$gid" = "0" ]; then
  echo "ATS_UID and ATS_GID must not be 0" >&2
  exit 1
fi
if ! getent group "$gid" >/dev/null 2>&1; then
  groupadd -o -g "$gid" trader
fi
gname=$(getent group "$gid" | cut -d: -f1)
if ! id trader >/dev/null 2>&1; then
  useradd -o -u "$uid" -g "$gname" -M -d /srv/trading -s /usr/sbin/nologin trader
fi
if [ -n "${CHRONY_GID:-}" ] && [ "$CHRONY_GID" != "$gid" ]; then
  if ! getent group "$CHRONY_GID" >/dev/null 2>&1; then
    groupadd -o -g "$CHRONY_GID" chronyhost
  fi
  cname=$(getent group "$CHRONY_GID" | cut -d: -f1)
  usermod -aG "$cname" trader
fi
for dir in state data logs intents reports alerts backups approvals notifications exports signals_archive; do
  mkdir -p "/srv/trading/$dir"
  chown "$uid:$gid" "/srv/trading/$dir"
done
exec gosu trader "$@"
