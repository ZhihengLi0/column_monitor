#!/bin/bash
# Cut over from the legacy cron/responder setup to the bfm daemon.
# Deliberate, one-shot, reversible (see rollback at the bottom). Run as cdms.
#
# What it does, in order:
#   1. install units (deploy/install.sh --no-start)
#   2. remove the legacy cron lines (monitor.py, --summary, @reboot responder,
#      ln2 ingest/monitor, arduino watchdog, legacy decision recorder)
#   3. stop the legacy responder and read_arduino processes
#   4. start ln2-reader + bluefors-monitor
#   5. verify: processes, two check cycles in the journal, state file sane
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
BACKUP=/mnt/harddrive/pi/backups/cutover_$(date +%Y%m%d_%H%M%S)
mkdir -p "$BACKUP"

echo "== 0. backups → $BACKUP =="
crontab -l > "$BACKUP/crontab.legacy" || true
cp "$REPO/monitor_state.json" "$BACKUP/monitor_state.legacy.json"
cp /home/cdms/ln2_monitor/monitor_state.json "$BACKUP/ln2_state.legacy.json" 2>/dev/null || true

echo "== 1. install units =="
"$HERE/install.sh" --no-start

echo "== 2. remove legacy cron lines =="
crontab -l | grep -vE "bluefors_monitor/monitor\.py|slack_responder\.py|ln2_monitor/(ingest|monitor)\.py|arduino_watchdog\.sh|legacy_decisions\.py|^# BlueFors Monitor|^# Daily summary" | crontab - || true
echo "remaining crontab:"; crontab -l | sed 's/^/    /' || echo "    (empty)"

echo "== 3. stop legacy processes =="
pkill -9 -f "slack_responder.py" || true
pkill -f "read_arduino.py" || true
pkill -f "tools/legacy_decisions.py" || true
pkill -f "bfm.cli shadow" || true
sleep 2
ps -eo comm,args | awk '$1=="python3" && /slack_responder|read_arduino|bluefors_monitor\/monitor\.py/' | sed 's/^/    still running: /' || true

echo "== 4. start services =="
sudo systemctl enable bluefors-monitor.service ln2-reader.service >/dev/null
sudo systemctl start ln2-reader.service
sudo systemctl start bluefors-monitor.service

echo "== 5. verify =="
sleep 75
systemctl --no-pager status bluefors-monitor.service ln2-reader.service | grep -E "●|Active:"
echo "--- last daemon cycles ---"
journalctl -u bluefors-monitor -n 20 --no-pager -o cat | grep -E "cycle|ERROR|CRITICAL|starting" | tail -8
echo "--- ln2 reader ---"
journalctl -u ln2-reader -n 5 --no-pager -o cat
echo "--- python processes ---"
ps -eo pid,etimes,comm,args | awk '$3=="python3"{print "   ",$1,$2"s",$5,$6}'
echo
echo "Cut-over done. Rollback if needed:"
echo "  sudo systemctl disable --now bluefors-monitor ln2-reader; crontab $BACKUP/crontab.legacy; cp $BACKUP/monitor_state.legacy.json $REPO/monitor_state.json"
