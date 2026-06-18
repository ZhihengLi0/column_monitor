#!/bin/bash
# Install cron jobs: sync data and check alerts every minute.
# Usage: bash setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing cron jobs from $SCRIPT_DIR ..."

# Export existing cron, strip old bluefors entries
crontab -l 2>/dev/null | grep -v "bluefors_monitor" > /tmp/bluefors_cron

cat >> /tmp/bluefors_cron <<EOF
# BlueFors Monitor
* * * * * python3 $SCRIPT_DIR/monitor.py >> $SCRIPT_DIR/monitor.log 2>&1
EOF

crontab /tmp/bluefors_cron
rm /tmp/bluefors_cron

echo "Done. Current cron:"
crontab -l | grep -A2 "BlueFors"
