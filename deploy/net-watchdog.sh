#!/bin/bash
# Network watchdog for the BlueFors monitor Pi.
#
# The Pi reaches the internet only through the UofM-Guest Wi-Fi. When that
# captive-portal session expires, every HTTPS call to slack.com is hijacked to
# the portal (SSL "Hostname mismatch") and the bot goes silent without anyone
# noticing (2026-11-10 → 11-20: 10 days). This runs from net-watchdog.timer
# every 2 minutes as root:
#   * probe https://slack.com/api/api.test with certificate verification
#   * after 3 consecutive failures, bounce the Wi-Fi connection (that is what
#     fixed it on 2026-11-20 after the reboot)
#   * if NetworkManager still reports a captive portal, retry only every
#     15 minutes and say so in the journal — a human has to accept the portal
#   * never reboot the machine
set -u
STATE_DIR=/run/net-watchdog
mkdir -p "$STATE_DIR"
FAIL_FILE="$STATE_DIR/failures"
LAST_BOUNCE="$STATE_DIR/last_bounce"
WIFI_CON="${WIFI_CON:-UofM-Guest}"
PROBE_URL="https://slack.com/api/api.test"
MAX_FAILS=3
BOUNCE_COOLDOWN=900      # seconds between bounces while still broken

log() { logger -t net-watchdog "$*"; echo "$(date '+%F %T') $*"; }

fails=$(cat "$FAIL_FILE" 2>/dev/null || echo 0)

if curl -fsS --max-time 15 "$PROBE_URL" -o /dev/null 2>/dev/null; then
    if [ "$fails" -gt 0 ]; then
        log "internet OK again after $fails failed probe(s)"
    fi
    echo 0 > "$FAIL_FILE"
    exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$FAIL_FILE"
conn=$(nmcli -t networking connectivity 2>/dev/null || echo unknown)
log "probe failed ($fails/$MAX_FAILS), connectivity=$conn"

[ "$fails" -lt "$MAX_FAILS" ] && exit 0

now=$(date +%s)
last=$(cat "$LAST_BOUNCE" 2>/dev/null || echo 0)
if [ $((now - last)) -lt "$BOUNCE_COOLDOWN" ]; then
    exit 0          # bounced recently; give it time
fi

log "bouncing Wi-Fi connection '$WIFI_CON' (connectivity=$conn)"
echo "$now" > "$LAST_BOUNCE"
nmcli con down "$WIFI_CON" >/dev/null 2>&1
sleep 5
nmcli con up "$WIFI_CON" >/dev/null 2>&1
sleep 20
conn=$(nmcli -t networking connectivity 2>/dev/null || echo unknown)
if curl -fsS --max-time 15 "$PROBE_URL" -o /dev/null 2>/dev/null; then
    log "Wi-Fi bounce restored internet access"
    echo 0 > "$FAIL_FILE"
elif [ "$conn" = "portal" ]; then
    log "STILL BEHIND CAPTIVE PORTAL after bounce — someone must accept the guest Wi-Fi portal on this Pi; will retry every $((BOUNCE_COOLDOWN/60)) min"
else
    log "still no internet after bounce (connectivity=$conn); will retry every $((BOUNCE_COOLDOWN/60)) min"
fi
