# BlueFors Monitor Configuration

# ── Remote Windows PC (BlueFors CS2) ──────────────────────────────────────
REMOTE_HOST = "172.31.255.10"
REMOTE_PG_PORT = 5432
REMOTE_PG_USER = "postgres"
REMOTE_PG_PASSWORD = "postgres"
REMOTE_PG_DB = "cs2"

# ── Local PostgreSQL (Raspberry Pi) ───────────────────────────────────────
LOCAL_PG_HOST = "localhost"
LOCAL_PG_PORT = 5432
LOCAL_PG_USER = "postgres"
LOCAL_PG_PASSWORD = "cs2monitor"
LOCAL_PG_DB = "cs2"

# ── Slack ─────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = ""   # Set your Slack Bot Token here (xoxb-...)
SLACK_CHANNEL = ""    # Channel name or ID, e.g. "#bluefors-alerts"

# ── Sync settings ──────────────────────────────────────────────────────────
SYNC_BATCH_SIZE = 5000   # Max rows pulled per table per sync cycle

# ── Alert thresholds ───────────────────────────────────────────────────────
# Temperatures in Kelvin, pressures in mbar
THRESHOLDS = {
    # sensor mapping        : (max_value, min_value, description)
    "MXC_TEMPERATURE":      (0.030,  None,  "MXC temperature > 30 mK"),
    "MXC_TEMPERATURE_FAR":  (0.050,  None,  "MXC far-end temperature > 50 mK"),
    "STILL_TEMPERATURE":    (2.0,    None,  "Still temperature > 2 K"),
    "4K_TEMPERATURE":       (6.0,    None,  "4K plate > 6 K"),
    "50K_TEMPERATURE":      (65.0,   None,  "50K plate > 65 K"),
    "B1A_TEMPERATURE":      (1.0,    None,  "B1A stage > 1 K"),
    "B2_TEMPERATURE":       (4.5,    None,  "B2 stage > 4.5 K"),
    "P1_PRESSURE":          (20.0,   None,  "P1 return pressure > 20 mbar"),
    "P2_PRESSURE":          (0.5,    None,  "P2 still pressure > 0.5 mbar"),
    "P5_PRESSURE":          (1e-3,   None,  "P5 MXC pressure > 1e-3 mbar"),
    "FLOW_VALUE":           (None,   0.01,  "He flow < 0.01 mmol/s"),
}

# Minutes before the same sensor can trigger another alert
ALERT_COOLDOWN_MINUTES = 30

# Minimum CS2 alert severity to forward to Slack (1=warning, 2=error)
CS2_ALERT_MIN_SEVERITY = 2
