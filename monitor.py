#!/usr/bin/env python3
"""
Alert monitor: checks local DB for sensor anomalies and CS2 alerts, sends Slack notifications.
Usage:
  python3 monitor.py         # normal run (called by cron)
  python3 monitor.py --init  # initialise state without sending historical alerts
"""

import sys
import json
import logging
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
from pathlib import Path

INIT_MODE = "--init" in sys.argv

sys.path.insert(0, str(Path(__file__).parent))
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monitor] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "monitor.log"),
    ],
)
log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "monitor_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_alert_time": {}, "last_cs2_alert_id": 0}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, default=str, indent=2))


def local_conn():
    kwargs = dict(
        host=config.LOCAL_PG_HOST,
        port=config.LOCAL_PG_PORT,
        user=config.LOCAL_PG_USER,
        dbname=config.LOCAL_PG_DB,
        connect_timeout=5,
    )
    if config.LOCAL_PG_PASSWORD:
        kwargs["password"] = config.LOCAL_PG_PASSWORD
    return psycopg2.connect(**kwargs)


def send_slack(message: str, color: str = "danger"):
    """Send a Slack message. color: danger (red) / warning (yellow) / good (green)"""
    if not config.SLACK_BOT_TOKEN or not config.SLACK_CHANNEL:
        log.warning(f"[SLACK NOT CONFIGURED] {message}")
        return
    payload = {
        "channel": config.SLACK_CHANNEL,
        "attachments": [{
            "color": color,
            "text": message,
            "footer": f"BlueFors Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        }],
    }
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
            timeout=10,
        )
        resp = r.json()
        if not resp.get("ok"):
            log.error(f"Slack API error: {resp.get('error')}")
    except Exception as e:
        log.error(f"Slack send failed: {e}")


def check_sensor_thresholds(conn, state: dict) -> list[str]:
    alerts = []
    now = datetime.now()
    cooldown = timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)

    mappings = list(config.THRESHOLDS.keys())
    placeholders = ",".join(["%s"] * len(mappings))

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (mapping)
                mapping, value, time
            FROM public.double_value_change_events
            WHERE mapping IN ({placeholders})
            ORDER BY mapping, time DESC
            """,
            mappings,
        )
        rows = cur.fetchall()

    for row in rows:
        name = row["mapping"]
        value = row["value"]
        ts = row["time"]
        max_val, min_val, desc = config.THRESHOLDS[name]

        triggered = False
        if max_val is not None and value > max_val:
            triggered = True
        elif min_val is not None and value < min_val:
            triggered = True

        if not triggered:
            continue

        last_str = state["last_alert_time"].get(name)
        if last_str:
            last_dt = datetime.fromisoformat(last_str)
            if now - last_dt < cooldown:
                continue

        state["last_alert_time"][name] = now.isoformat()
        unit = "K" if "TEMPERATURE" in name else ("mbar" if "PRESSURE" in name else "")
        msg = f":warning: *{desc}*\nCurrent value: `{value:.4g} {unit}` | Sensor time: {ts}"
        alerts.append(msg)
        log.warning(f"Threshold alert: {name} = {value:.4g}")

    return alerts


def check_cs2_alerts(conn, state: dict) -> list[str]:
    alerts = []
    last_id = state.get("last_cs2_alert_id", 0)

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT id, code, datetime, title, description, severity
            FROM public.alerts
            WHERE id > %s AND severity >= %s
            ORDER BY id ASC
            LIMIT 50
            """,
            (last_id, config.CS2_ALERT_MIN_SEVERITY),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    state["last_cs2_alert_id"] = max(row["id"] for row in rows)

    from collections import defaultdict
    by_code: dict[int, list] = defaultdict(list)
    for row in rows:
        by_code[row["code"]].append(row)

    for code, group in by_code.items():
        row = group[0]
        sev_emoji = ":red_circle:" if row["severity"] >= 2 else ":yellow_circle:"
        sev_text = "Error" if row["severity"] >= 2 else "Warning"
        count_note = f" (x{len(group)})" if len(group) > 1 else ""
        msg = (
            f"{sev_emoji} *CS2 System {sev_text}* [Code {code}]{count_note}\n"
            f"*{row['title']}*\n"
            f"{row['description'] or ''}\n"
            f"First: {group[0]['datetime']}  Last: {group[-1]['datetime']}"
        )
        alerts.append(msg)
        log.warning(f"CS2 alert x{len(group)}: [{code}] {row['title']}")

    return alerts


def check_data_freshness(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(time) FROM public.double_value_change_events")
        latest = cur.fetchone()[0]

    if latest is None:
        return ":sos: No sensor data found in local database!"

    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - latest
    if age > timedelta(minutes=5):
        minutes = int(age.total_seconds() / 60)
        return f":sos: *Data sync may have stopped!* Latest data is {minutes} minutes old. Please check the sync script."
    return None


def init_state(conn) -> dict:
    state: dict = {"last_alert_time": {}, "last_cs2_alert_id": 0}

    with conn.cursor() as cur:
        cur.execute("SELECT MAX(id) FROM public.alerts WHERE severity >= %s",
                    (config.CS2_ALERT_MIN_SEVERITY,))
        row = cur.fetchone()
        state["last_cs2_alert_id"] = row[0] or 0

    now = datetime.now()
    for mapping in config.THRESHOLDS:
        state["last_alert_time"][mapping] = now.isoformat()

    log.info(f"Initialised: last_cs2_alert_id={state['last_cs2_alert_id']}, "
             f"skipped historical alerts for {len(config.THRESHOLDS)} sensors")
    return state


def run():
    state = load_state()
    all_alerts = []

    try:
        conn = local_conn()
    except Exception as e:
        log.error(f"Cannot connect to local database: {e}")
        if not INIT_MODE:
            send_slack(f":sos: *BlueFors Monitor* cannot connect to local database: {e}")
        return

    try:
        if INIT_MODE:
            state = init_state(conn)
            save_state(state)
            log.info("--init complete. Next run will detect new alerts only.")
            return

        freshness_alert = check_data_freshness(conn)
        if freshness_alert:
            all_alerts.append(freshness_alert)

        sensor_alerts = check_sensor_thresholds(conn, state)
        all_alerts.extend(sensor_alerts)

        cs2_alerts = check_cs2_alerts(conn, state)
        all_alerts.extend(cs2_alerts)

    finally:
        conn.close()

    for msg in all_alerts:
        send_slack(msg)

    save_state(state)

    if all_alerts:
        log.info(f"Sent {len(all_alerts)} alert(s)")
    else:
        log.debug("No alerts")


if __name__ == "__main__":
    run()
