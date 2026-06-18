#!/usr/bin/env python3
"""
Incremental sync from Windows BlueFors CS2 database to local Raspberry Pi PostgreSQL.
Usage: python3 sync.py  (called by cron every minute)
"""

import sys
import json
import logging
import psycopg2
import psycopg2.extras
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sync] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "sync.log"),
    ],
)
log = logging.getLogger(__name__)

TABLES = {
    "double_value_change_events":  ("time", "id"),
    "int_value_change_events":     ("time", "id"),
    "boolean_value_change_events": ("time", "id"),
    "string_value_change_events":  ("time", "id"),
    "json_value_change_events":    ("time", "id"),
    "device_events":               ("time", "id"),
    "alerts":                      ("datetime", "id"),
    "automation_events":           ("datetime", "id"),
    "user_log_entries":            ("created_datetime", "id"),
}

STATE_FILE = Path(__file__).parent / "sync_state.json"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, default=str, indent=2))


def remote_conn():
    return psycopg2.connect(
        host=config.REMOTE_HOST,
        port=config.REMOTE_PG_PORT,
        user=config.REMOTE_PG_USER,
        password=config.REMOTE_PG_PASSWORD,
        dbname=config.REMOTE_PG_DB,
        connect_timeout=10,
    )


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


def sync_table(table, ts_col, id_col, remote, local, state) -> int:
    last_id = state.get(table, {}).get("last_id", 0)

    with remote.cursor(cursor_factory=psycopg2.extras.DictCursor) as rcur:
        rcur.execute(
            f"SELECT * FROM public.{table} WHERE {id_col} > %s "
            f"ORDER BY {id_col} LIMIT %s",
            (last_id, config.SYNC_BATCH_SIZE),
        )
        rows = rcur.fetchall()

    if not rows:
        return 0

    cols = [desc[0] for desc in rcur.description]
    placeholders = ",".join(["%s"] * len(cols))
    col_list = ",".join(f'"{c}"' for c in cols)
    insert_sql = (
        f"INSERT INTO public.{table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({id_col}) DO NOTHING"
    )

    values = [tuple(row[c] for c in cols) for row in rows]
    with local.cursor() as lcur:
        psycopg2.extras.execute_batch(lcur, insert_sql, values, page_size=500)
    local.commit()

    state.setdefault(table, {})["last_id"] = max(row[id_col] for row in rows)
    return len(rows)


def sync_device_states(remote, local):
    with remote.cursor(cursor_factory=psycopg2.extras.DictCursor) as rcur:
        rcur.execute("SELECT * FROM public.device_states")
        rows = rcur.fetchall()
    if not rows:
        return
    cols = [desc[0] for desc in rcur.description]
    placeholders = ",".join(["%s"] * len(cols))
    col_list = ",".join(f'"{c}"' for c in cols)
    update_set = ",".join(
        f'"{c}"=EXCLUDED."{c}"' for c in cols if c != "device_id"
    )
    sql = (
        f"INSERT INTO public.device_states ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT (device_id) DO UPDATE SET {update_set}"
    )
    values = [tuple(row[c] for c in cols) for row in rows]
    with local.cursor() as lcur:
        psycopg2.extras.execute_batch(lcur, sql, values, page_size=100)
    local.commit()


def run():
    state = load_state()
    total = 0
    errors = []

    try:
        rconn = remote_conn()
        log.info("Remote DB connected")
    except Exception as e:
        log.error(f"Cannot connect to remote DB: {e}")
        return False

    try:
        lconn = local_conn()
    except Exception as e:
        log.error(f"Cannot connect to local DB: {e}")
        rconn.close()
        return False

    try:
        for table, (ts_col, id_col) in TABLES.items():
            try:
                n = sync_table(table, ts_col, id_col, rconn, lconn, state)
                if n:
                    log.info(f"  {table}: +{n} rows")
                    total += n
            except Exception as e:
                log.error(f"  {table}: {e}")
                errors.append(table)
                lconn.rollback()

        try:
            sync_device_states(rconn, lconn)
        except Exception as e:
            log.error(f"  device_states: {e}")

    finally:
        rconn.close()
        lconn.close()

    save_state(state)
    log.info(f"Sync done: +{total} rows, {len(errors)} errors")
    return len(errors) == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
