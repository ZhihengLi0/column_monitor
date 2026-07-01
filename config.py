# BlueFors Monitor Configuration

# ── Secrets (loaded from config_secret.py on the local machine) ───────────────
try:
    from config_secret import LOCAL_PG_PASSWORD, SLACK_BOT_TOKEN
except ImportError:
    LOCAL_PG_PASSWORD = "YOUR_PG_PASSWORD"
    SLACK_BOT_TOKEN   = "YOUR_SLACK_BOT_TOKEN"

# ── Local PostgreSQL (Raspberry Pi) ───────────────────────────────────────────
LOCAL_PG_HOST = "localhost"
LOCAL_PG_PORT = 5432
LOCAL_PG_USER = "postgres"
LOCAL_PG_DB   = "cs2"

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_CHANNEL     = "C0B42G4AU0N"
SLACK_BOT_USER_ID = "U0BBGRB0HC4"

# ── Operating mode detection ───────────────────────────────────────────────────
# The 50K plate temperature is used to determine which mode the fridge is in.
#
#   50K_TEMPERATURE > MODE_IDLE_ABOVE_K   →  IDLE        (room temperature, not running)
#   50K_TEMPERATURE < MODE_COLD_BELOW_K   →  COLD        (operational, base temperature)
#   between the two                        →  TRANSITIONING (cooling down / warming up)
#
# During TRANSITIONING, all threshold alerts are suppressed to avoid spam.
# Only CS2 system alerts and data-freshness alerts are sent.

MODE_DETECTION_SENSOR = "50K_TEMPERATURE"
MODE_IDLE_ABOVE_K     = 200.0   # K — above this the system is clearly at room temperature
MODE_COLD_BELOW_K     = 80.0    # K — below this the 50K plate is stabilised, system is cold

# ── Thresholds: IDLE mode (room temperature, fridge not running) ───────────────
# Check that pressures and other values are reasonable while the system sits idle.
# Temperatures are NOT checked here — they are expected to be near room temperature.
THRESHOLDS_IDLE = {
    # sensor mapping  : (max_value, min_value, description)
    # 4-element tuple : (max_value, min_value, max_desc, min_desc)
    # Database stores pressure in bar; thresholds are in bar (1 mbar = 0.001 bar)
    "P2_PRESSURE":  (0.01,  None, "P2 pressure unusually high at room temperature (> 10 mbar)"),
    "P5_PRESSURE":  (1e-4,  None, "P5 pressure unusually high at room temperature (> 0.1 mbar)"),
    "P1_PRESSURE":  (None,  1e-5,
                    None,
                    "P1 pressure < 0.01 mbar at room temperature — possible leak, please perform a leak check"),
}

# ── Thresholds: COLD mode (fridge operational, base temperature) ───────────────
# These are checked when 50K_TEMPERATURE < MODE_COLD_BELOW_K.
THRESHOLDS_COLD = {
    # sensor mapping          : (max_value, min_value, description)
    # Temperatures in K (database native unit)
    "MXC_TEMPERATURE":     (0.030,  None,  "MXC temperature > 30 mK"),
    "MXC_TEMPERATURE_FAR": (0.050,  None,  "MXC far-end temperature > 50 mK"),
    "STILL_TEMPERATURE":   (2.0,    None,  "Still temperature > 2 K"),
    "4K_TEMPERATURE":      (6.0,    None,  "4K plate > 6 K"),
    "50K_TEMPERATURE":     (65.0,   None,  "50K plate > 65 K"),
    "B1A_TEMPERATURE":     (1.0,    None,  "B1A stage > 1 K"),
    "B2_TEMPERATURE":      (4.5,    None,  "B2 stage > 4.5 K"),
    # Pressures in bar (database native unit); 1 mbar = 0.001 bar
    "P1_PRESSURE":         (0.02,   None,  "P1 return pressure > 20 mbar"),
    "P2_PRESSURE":         (5e-4,   None,  "P2 still pressure > 0.5 mbar"),
    "P5_PRESSURE":         (1e-6,   None,  "P5 MXC pressure > 1e-3 mbar"),
    "FLOW_VALUE":          (None,   0.01,  "He flow < 0.01 mmol/s"),
}

# Backwards-compatible alias
THRESHOLDS = THRESHOLDS_COLD

# ── Alert behaviour ────────────────────────────────────────────────────────────
# Minutes before the same sensor can trigger another alert
ALERT_COOLDOWN_MINUTES = 30

# Minimum CS2 alert severity to forward to Slack (1 = warning, 2 = error only)
CS2_ALERT_MIN_SEVERITY = 2

# Sync batch size (rows per table per sync cycle, Windows side)
SYNC_BATCH_SIZE = 5000
