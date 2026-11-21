#!/bin/bash
# Install / update the bfm services on the Pi. Idempotent. Run as cdms (uses sudo).
#
#   deploy/install.sh            install units + logrotate, (re)start the daemon
#   deploy/install.sh --no-start install only
#
# Cut-over from the legacy cron setup is a separate, deliberate step (see
# deploy/cutover.sh) — this script does not touch the crontab.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"

echo "== syntax / tests =="
(cd "$REPO" && python3 -m pytest -q tests >/dev/null && echo "tests OK")

echo "== systemd units =="
sudo install -m 644 "$HERE/bluefors-monitor.service" /etc/systemd/system/bluefors-monitor.service
sudo install -m 644 "$HERE/ln2-reader.service"       /etc/systemd/system/ln2-reader.service
sudo install -m 644 "$HERE/net-watchdog.service"     /etc/systemd/system/net-watchdog.service
sudo install -m 644 "$HERE/net-watchdog.timer"       /etc/systemd/system/net-watchdog.timer
sudo install -m 644 "$HERE/logrotate-bfm"            /etc/logrotate.d/bfm
sudo systemctl daemon-reload
sudo systemctl enable net-watchdog.timer >/dev/null
sudo systemctl start  net-watchdog.timer
mkdir -p /mnt/harddrive/pi/logs

if [ "${1:-}" != "--no-start" ]; then
    echo "== (re)starting services =="
    sudo systemctl enable bluefors-monitor.service ln2-reader.service >/dev/null
    sudo systemctl restart ln2-reader.service
    sudo systemctl restart bluefors-monitor.service
    sleep 3
    systemctl --no-pager --lines=0 status bluefors-monitor.service ln2-reader.service | grep -E "●|Active:"
fi
echo "done"
