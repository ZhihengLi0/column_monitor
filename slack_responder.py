#!/usr/bin/env python3
"""
Slack command responder — runs as a persistent service, polls every 5 seconds.
Handles all @BlueFors-Alert interactive commands immediately.
monitor.py handles alerts; this handles commands. They share monitor_state.json.
"""

import time
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
from monitor import (
    check_commands, load_state, save_state, local_conn
)

# Re-configure logging after monitor import (monitor.py sets the root logger)
LOG_FILE = Path(__file__).parent / "responder.log"
root = logging.getLogger()
root.handlers.clear()
root.addHandler(logging.FileHandler(LOG_FILE))
root.setLevel(logging.INFO)
logging.Formatter.default_msec_format = "%s,%03d"
for h in root.handlers:
    h.setFormatter(logging.Formatter("%(asctime)s [responder] %(levelname)s %(message)s"))
log = logging.getLogger("responder")

POLL_INTERVAL = 5  # seconds


def run():
    log.info("Slack responder started (polling every %ds)", POLL_INTERVAL)
    while True:
        try:
            state = load_state()
            conn  = local_conn()
            check_commands(state, conn)
            conn.close()
            save_state(state)
        except Exception as e:
            log.error("Error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
