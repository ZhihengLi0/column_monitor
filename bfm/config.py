"""bfm configuration.

Everything operator-tunable lives here (same names and values as the legacy
config.py, plus the LN2 section that used to live in ln2_monitor/ln2_config.py).
Secrets come from config_secret.py in the repo root (git-ignored).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from config_secret import LOCAL_PG_PASSWORD, SLACK_BOT_TOKEN   # noqa: F401
except ImportError:  # fresh checkout / tests
    LOCAL_PG_PASSWORD = None
    SLACK_BOT_TOKEN = None

# ── Paths ──────────────────────────────────────────────────────────────────────
STATE_FILE   = ROOT / "monitor_state.json"
LOG_DIR      = Path("/mnt/harddrive/pi/logs")      # falls back to ROOT/logs if unmounted
RUNTIME_DIR  = Path("/run/bluefors")               # plots / temp files (systemd RuntimeDirectory)
NLP_EXAMPLES = ROOT / "nlp_user_examples.jsonl"

# ── Local PostgreSQL (Raspberry Pi) ───────────────────────────────────────────
LOCAL_PG_HOST = "localhost"
LOCAL_PG_PORT = 5432
LOCAL_PG_USER = "postgres"
LOCAL_PG_DB   = "cs2"
LN2_PG_DB     = "ln2"        # same server, separate database

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_CHANNEL     = "C0B42G4AU0N"
SLACK_BOT_USER_ID = "U0BBGRB0HC4"

# ── Daemon timing ─────────────────────────────────────────────────────────────
CHECK_INTERVAL_S      = 60     # alarm/event/reminder cycle
COMMAND_POLL_S        = 5      # Slack @mention polling
REPLY_POLL_S          = 20     # ack / reminder-confirmation polling (2 GETs per open post)
SENDER_PACE_S         = 1.0    # minimum gap between outgoing posts
COMMAND_MAX_AGE_MIN   = 15     # ignore @mentions older than this (post-outage replay guard)
SLACK_OFFLINE_DIGEST_MIN = 10  # after an outage longer than this, post a digest first
SUMMARY_HOURS         = (8, 20)   # local time; 12-hour summary posts
SUMMARY_MAX_LATE_MIN  = 60     # a missed summary slot is sent if less than this late

# ── Operating mode detection ───────────────────────────────────────────────────
#   50K_TEMPERATURE > MODE_IDLE_ABOVE_K   →  IDLE
#   50K_TEMPERATURE < MODE_COLD_BELOW_K   →  COLD
#   between                               →  TRANSITIONING (threshold alerts suppressed)
MODE_DETECTION_SENSOR = "50K_TEMPERATURE"
MODE_IDLE_ABOVE_K     = 200.0
MODE_COLD_BELOW_K     = 80.0

# ── Thresholds: IDLE mode ──────────────────────────────────────────────────────
# mapping: (max_value, min_value, description) or (max, min, max_desc, min_desc)
# Pressures are in bar (DB native); 1 mbar = 0.001 bar.
THRESHOLDS_IDLE = {
    "P2_PRESSURE":  (0.01,  None, "P2 pressure unusually high at room temperature (> 10 mbar)"),
    "P5_PRESSURE":  (1e-4,  None, "P5 pressure unusually high at room temperature (> 0.1 mbar)"),
    "P1_PRESSURE":  (None,  1e-5, None,
                     "P1 pressure < 0.01 mbar at room temperature — possible leak, please perform a leak check"),
}

# ── Thresholds: COLD mode ──────────────────────────────────────────────────────
THRESHOLDS_COLD = {
    "MXC_TEMPERATURE":     (0.030,  None,  "MXC temperature > 30 mK"),
    "MXC_TEMPERATURE_FAR": (0.050,  None,  "MXC far-end temperature > 50 mK"),
    "STILL_TEMPERATURE":   (2.0,    None,  "Still temperature > 2 K"),
    "4K_TEMPERATURE":      (6.0,    None,  "4K plate > 6 K"),
    "50K_TEMPERATURE":     (65.0,   None,  "50K plate > 65 K"),
    "B1A_TEMPERATURE":     (315.0,  None,  "B1A pump temperature > 315 K (overheat)"),
    "B2_TEMPERATURE":      (315.0,  None,  "B2 pump temperature > 315 K (overheat)"),
    "P1_PRESSURE":         (0.02,   None,  "P1 return pressure > 20 mbar"),
    "P2_PRESSURE":         (5e-4,   None,  "P2 still pressure > 0.5 mbar"),
    "P5_PRESSURE":         (0.01,   None,  "P5 MXC pressure > 0.01 bar (10 mbar)"),
    "FLOW_VALUE":          (0.002,  None,  "He flow > 0.002 mmol/s"),
}
THRESHOLDS = THRESHOLDS_COLD   # backwards-compatible alias

# ── Alert behaviour ────────────────────────────────────────────────────────────
ALERT_COOLDOWN_MINUTES = 30
ACK_DEFAULT_MINUTES    = 10
CS2_ALERT_MIN_SEVERITY = 2     # 1 = warning, 2 = error only
ACK_REACTIONS = {"white_check_mark", "heavy_check_mark", "clap", "+1", "ok_hand"}

# ── Chilled water filter replacement reminder ──────────────────────────────────
FILTER_INTERVAL_DAYS           = 21
FILTER_REMINDER_INTERVAL_HOURS = 24

# ── Weekly eyewash-equipment check reminder ───────────────────────────────────
# Fixed slot: EYEWASH_START_WEEKDAY (0 = Monday … 4 = Friday) at EYEWASH_START_HOUR
# local time; the "week" runs from one slot to the next. Nags every
# EYEWASH_REMINDER_INTERVAL_HOURS until someone replies "yes" under the latest post.
EYEWASH_SLACK_USER_ID           = "U0B44KPDXQA"   # James
EYEWASH_START_WEEKDAY           = 4
EYEWASH_START_HOUR              = 12
EYEWASH_REMINDER_INTERVAL_HOURS = 6

# ── GHS compressed-air pressure alarms (all modes) ─────────────────────────────
AIR_PRESSURE_DEVICE = "plc.GHSDiagnostics"
AIR_PRESSURE_ALARMS = {
    "fInputAirPressure": {
        "label": "Compressed Air — Input Pressure",
        "normal": 690_000, "warn_below": 620_000, "crit_below": 540_000,
        "unit": "kPa", "scale": 0.001,
    },
    "fRegulatorAirPressure": {
        "label": "Compressed Air — Regulator Pressure",
        "normal": 492_000, "warn_below": None, "crit_below": 479_000,
        "unit": "kPa", "scale": 0.001,
    },
}

# ── Pulse-tube compressor temperature limits (only while running) ──────────────
PT_DEVICE            = "plc.Pulsetube1"
PT_COOLANT_IN_HIGH_C = 17.0
PT_OIL_HIGH_C        = 40.0

# ── Device health alarms (all devices in device_states, every mode) ────────────
DEVICE_FLAG_IGNORE = ("underrange", "overrange")

# ── Cool-down milestone notifications (informational) ──────────────────────────
COOLDOWN_MILESTONES = [("4K_TEMPERATURE", 10.0, "4K plate")]
COOLDOWN_MILESTONE_HYSTERESIS_K = 1.0

# ── Data freshness (whole pipeline) ────────────────────────────────────────────
FRESHNESS_MAX_MINUTES = 5

# ── Per-sensor data staleness (all modes) ──────────────────────────────────────
STALENESS_WARM_MINUTES = 1440   # flat limit for every sensor while IDLE (warm stage)
STALENESS_SENSORS = [
    # (mapping, label, max_age_minutes, confidence "certain"/"maybe")
    ("MXC_TEMPERATURE_FAR", "MXC2",   5, "certain"),
    ("STILL_TEMPERATURE",   "Still",  5, "certain"),
    ("4K_TEMPERATURE",      "4K",     5, "certain"),
    ("50K_TEMPERATURE",     "50K",   10, "certain"),
    ("P2_PRESSURE",         "P2",    60, "maybe"),
    ("P3_PRESSURE",         "P3",    60, "maybe"),
    ("P4_PRESSURE",         "P4",    60, "maybe"),
    ("P5_PRESSURE",         "P5",    60, "maybe"),
    ("P6_PRESSURE",         "P6",    60, "maybe"),
    ("P7_PRESSURE",         "P7",    60, "maybe"),
    ("B2_TEMPERATURE",      "B2",    60, "maybe"),   # only while B2 pump is ON
]
# Global-outage guard: if EVERY sensor is older than this, it's a pipeline
# outage (FRESHNESS alarm), not individual sensors.
STALENESS_GLOBAL_GUARD_MIN = 5

# ── Pulse-tube analog recording (for `plot coolant in` etc.) ───────────────────
PULSETUBE_TABLE          = "public.pulsetube_readings"
PULSETUBE_RETENTION_DAYS = 365

# ── LN2 scale (merged from ln2_monitor/ln2_config.py) ──────────────────────────
LN2_WEIGHT_UNIT        = "kg"
LN2_WEIGHT_MAX_JUMP    = 5.0      # reader: reject jumps larger than this between readings
LN2_WEIGHT_LOW         = -2.5     # alert when weight < this (time to refill)
LN2_WEIGHT_CRITICAL    = None
LN2_TEMP_HIGH          = None
LN2_TEMP_LOW           = None
LN2_DEW_POINT_HIGH     = None
LN2_DATA_STALE_MINUTES = 10
LN2_ARDUINO_HOST       = "172.31.255.80"
LN2_ARDUINO_PORT       = 5000

# Sync batch size (rows per table per sync cycle, Windows side)
SYNC_BATCH_SIZE = 5000
