# BlueFors CS2 Monitor

Real-time monitoring and Slack alerting for a BlueFors dilution refrigerator (CS2 control system).  
Data is synced every minute from the Windows CS2 PC to a Raspberry Pi, which runs anomaly detection and sends Slack notifications.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Network](#network)
3. [Operating Modes](#operating-modes)
4. [Raspberry Pi Setup](#raspberry-pi-setup)
5. [Database Backup Restore](#database-backup-restore)
6. [Windows Sync Script — sync_push.ps1](#windows-sync-script--sync_pushps1)
7. [Monitor Script — monitor.py](#monitor-script--monitorpy)
8. [Configuration — config.py](#configuration--configpy)
9. [Slack App Setup](#slack-app-setup)
10. [Starting the System](#starting-the-system)
11. [Slack Interaction](#slack-interaction)
12. [NLP Natural Language Interface](#nlp-natural-language-interface)
13. [Database Schema](#database-schema)
14. [Sensor Mappings](#sensor-mappings)
15. [Troubleshooting](#troubleshooting)
16. [File Reference](#file-reference)

**Appendix**
- [A — CS2 System & Database Background](#appendix-a--cs2-system--database-background)
- [B — Full Database Schema Reference](#appendix-b--full-database-schema-reference)
- [C — Data Flow & Migration Notes](#appendix-c--data-flow--migration-notes)

---

## Architecture

```
Windows PC (BlueFors CS2)                    Raspberry Pi
┌──────────────────────────────┐             ┌────────────────────────────────┐
│  CS2 Control Software        │             │  PostgreSQL 17  (port 5432)    │
│  PostgreSQL 14.9 (port 5434) │             │  /mnt/harddrive/cs2_database   │
│                              │   TCP 5432  │                                │
│  sync_push.ps1  ─────────────┼────────────►│  cs2 database  (mirror)        │
│  Windows Task Scheduler      │             │                                │
│  every 1 minute              │             │  monitor.py  ──────────────────┼──► Slack
└──────────────────────────────┘             │  cron, every 1 minute          │
                                             └────────────────────────────────┘
```

**Why Windows pushes to Pi instead of Pi pulling from Windows:**  
The Pi cannot initiate a TCP connection to the Windows machine (no open inbound port on Windows).  
The Windows machine can reach the Pi on port 5432. So the PowerShell script runs as a Windows Scheduled Task and pushes data outward every minute.

---

## Network

| Machine | IP | OS | Role |
|---|---|---|---|
| BlueFors CS2 PC | 172.31.255.10 | Windows | Data source (CS2 + PostgreSQL 14.9 on port 5434) |
| Raspberry Pi | 172.31.255.62 | Raspberry Pi OS 64-bit | Monitor hub (PostgreSQL 17 on port 5432) |

---

## Operating Modes

The fridge is not always running. To avoid sending meaningless alerts while the system is at room temperature or in the middle of a cool-down, the monitor automatically detects which state the fridge is in and applies a different set of checks for each.

### The three modes

```
50K plate temperature
        │
        ├─ > 200 K ──────────────────► IDLE          (room temperature, fridge not running)
        │
        ├─ between 80 K and 200 K ───► TRANSITIONING (cooling down or warming up)
        │
        └─ < 80 K ───────────────────► COLD          (fridge operational at base temperature)
```

| Mode | Emoji | Trigger | What is monitored |
|---|---|---|---|
| **IDLE** | ⚪ | 50K plate > 200 K | Idle pressure checks + cold cathode + CS2 system alerts |
| **TRANSITIONING** | 🔵 | 50K plate between 80 K and 200 K | Pulse-tube critical check (see below) + CS2 system alerts; threshold alerts suppressed |
| **COLD** | 🟢 | 50K plate < 80 K | Full sensor threshold monitoring + cold cathode |

> **Always on (every mode):** device on/off changes, R1A pump, valves V112/V113/V114, CS2 alerts, cold-cathode, data-freshness, **compressed-air pressure**, and **per-device health faults** (see [Device Health Monitoring](#device-health-monitoring)).

### Why TRANSITIONING suppresses alerts

During cool-down and warm-up the temperatures and pressures pass through a huge range of values. Without suppression, the monitor would send hundreds of threshold alerts as sensors cross their limits on the way to base temperature. TRANSITIONING mode keeps Slack quiet and only forwards genuine CS2 system errors.

### TRANSITIONING: pulse-tube critical check + direction

TRANSITIONING is split by **direction**, auto-detected from the 50K-plate trend over ~15 minutes (falling = *cool down*, rising = *warm up*). In either direction the **Pulse Tube must stay ON** — if it turns OFF, a **CRITICAL** alert fires (state-based, so it fires even if it was already off, and repeats every cooldown until fixed). The detected direction is shown in the alert, in `status`, and in the alarm listing. Requirements are configurable per direction via `TRANSITIONING_REQUIRED` in `monitor.py`.

### Mode change notifications

Every time the mode changes, the monitor sends a Slack message:

```
🔵 Mode changed: IDLE → TRANSITIONING
System is cooling down or warming up.
Threshold alerts suppressed — only CS2 system alerts forwarded.
```

### Manual mode override via Slack

If the auto-detection is wrong (e.g. a sensor gives a bad reading), anyone in the Slack channel can override the mode manually:

```
@BlueFors-Alert set mode idle          force IDLE mode
@BlueFors-Alert set mode cold          force COLD mode
@BlueFors-Alert set mode auto          return to automatic detection
@BlueFors-Alert mode                   show current mode and what is being monitored
```

### IDLE mode thresholds (room temperature)

These are the checks applied when the fridge is sitting at room temperature and not running.  
Only pressures are checked — temperatures are expected to be near room temperature and are not alerted.

| Sensor | Alert condition | Why |
|---|---|---|
| P2_PRESSURE | > 10 mbar | Unexpectedly high still pressure at rest |
| P5_PRESSURE | > 0.1 mbar | Unexpectedly high MXC pressure at rest |

### COLD mode thresholds (operational)

These are applied when the 50K plate is below 80 K, meaning the fridge has cooled significantly and is approaching or at base temperature.

| Sensor | Alert condition | Description |
|---|---|---|
| MXC_TEMPERATURE | > 30 mK | Mixing chamber too warm |
| MXC_TEMPERATURE_FAR | > 50 mK | MXC far-end too warm |
| STILL_TEMPERATURE | > 2 K | Still too warm |
| 4K_TEMPERATURE | > 6 K | 4K plate too warm |
| 50K_TEMPERATURE | > 65 K | 50K plate too warm |
| B1A_TEMPERATURE | > 1 K | B1A stage too warm |
| B2_TEMPERATURE | > 4.5 K | B2 stage too warm |
| P1_PRESSURE | > 20 mbar | Return line pressure too high |
| P2_PRESSURE | > 0.5 mbar | Still pressure too high |
| P5_PRESSURE | > 1e-3 mbar | MXC pressure too high |
| FLOW_VALUE | < 0.01 mmol/s | Helium flow too low |

> All thresholds can be adjusted at any time via Slack commands without restarting anything — see [Slack Interaction](#slack-interaction).

---

## Raspberry Pi Setup

### 1. Install PostgreSQL 17

```bash
sudo apt update
sudo apt install -y postgresql
```

Verify the version:

```bash
psql --version
# postgresql 17.x
```

### 2. Move data directory to external hard drive

The CS2 database is large. Store it on the mounted external drive instead of the SD card.

Edit `/etc/postgresql/17/main/postgresql.conf`:

```
data_directory = '/mnt/harddrive/cs2_database/main'
```

Copy the existing data directory to the drive, then restart:

```bash
sudo systemctl stop postgresql
sudo rsync -av /var/lib/postgresql/17/main/ /mnt/harddrive/cs2_database/main/
sudo chown -R postgres:postgres /mnt/harddrive/cs2_database/
sudo systemctl start postgresql
```

### 3. Allow remote connections

Edit `/etc/postgresql/17/main/postgresql.conf`:

```
listen_addresses = '*'
```

Edit `/etc/postgresql/17/main/pg_hba.conf` — add this line (allows the whole local subnet):

```
host    cs2    postgres    172.31.255.0/16    md5
```

Set the `postgres` user password:

```bash
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'cs2monitor';"
```

Restart PostgreSQL to apply:

```bash
sudo systemctl restart postgresql
```

### 4. Open the firewall

```bash
sudo ufw allow from 172.31.255.0/16 to any port 5432
sudo ufw status    # verify: port 5432 appears in the list
```

### 5. Create the database

```bash
sudo -u postgres psql -c "CREATE DATABASE cs2;"
```

### 6. Install Python dependencies

```bash
pip3 install psycopg2-binary requests matplotlib scikit-learn numpy
```

> `matplotlib` is required for plot images. `scikit-learn` and `numpy` are required for the NLP classifier.

---

## Database Backup Restore

The initial data was exported from the Windows CS2 machine using `pg_dump` and restored on the Pi.

**On the Windows machine** — export the CS2 database:

```powershell
# Run in PowerShell on the Windows CS2 machine
$env:PGPASSWORD = "postgres"
& "C:\Program Files\PostgreSQL\14\bin\pg_dump.exe" `
    -h localhost -p 5434 -U postgres -d cs2 `
    -F c -f "C:\bluefors_monitor\cs2_backup.dump"
```

**Copy the file to the Pi** (run from a machine that can reach both, or use a USB drive):

```bash
scp cdms@172.31.255.10:"C:/bluefors_monitor/cs2_backup.dump" /home/cdms/
```

**Restore on the Pi:**

```bash
export PGPASSWORD=cs2monitor
pg_restore -h localhost -U postgres -d cs2 -v /home/cdms/cs2_backup.dump
```

Verify the restore:

```bash
PGPASSWORD=cs2monitor psql -h localhost -U postgres -d cs2 \
    -c "SELECT COUNT(*) FROM double_value_change_events;"
# Should show ~3.6 million rows
```

---

## Windows Sync Script — sync_push.ps1

Place this file at `C:\bluefors_monitor\sync_push.ps1` on the Windows CS2 machine.

**How to copy it from the Pi to Windows:**

```powershell
# Run in PowerShell on the Windows machine
scp cdms@172.31.255.62:/home/cdms/bluefors_monitor/sync_push.ps1 C:\bluefors_monitor\sync_push.ps1
```

### Full code

```powershell
# BlueFors CS2 -> Raspberry Pi data sync script
# Uses psql.exe (bundled with PostgreSQL) - no Python or extra installs needed.
# Compatible with Windows PowerShell 5.1+
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1           # run once
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1 -Install  # register scheduled task

param([switch]$Install)

# --- Configuration ---
$LocalHost  = "localhost"
$LocalPort  = 5434          # CS2's PostgreSQL (NOT the default 5432)
$LocalUser  = "postgres"
$LocalPass  = "postgres"
$LocalDB    = "cs2"

$RemoteHost = "172.31.255.62"
$RemotePort = 5432
$RemoteUser = "postgres"
$RemotePass = "cs2monitor"
$RemoteDB   = "cs2"

$StateFile = Join-Path $PSScriptRoot "win_sync_state.json"
$LogFile   = Join-Path $PSScriptRoot "win_sync.log"
$BatchSize = 5000

$tables = @(
    "double_value_change_events",
    "int_value_change_events",
    "boolean_value_change_events",
    "string_value_change_events",
    "json_value_change_events",
    "device_events",
    "alerts",
    "automation_events",
    "user_log_entries"
)

# --- Locate psql.exe ---
$psqlPaths = @(
    "C:\Program Files\PostgreSQL\17\bin\psql.exe",
    "C:\Program Files\PostgreSQL\16\bin\psql.exe",
    "C:\Program Files\PostgreSQL\15\bin\psql.exe",
    "C:\Program Files\PostgreSQL\14\bin\psql.exe",
    "C:\Program Files\PostgreSQL\13\bin\psql.exe"
)
$psql = $psqlPaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $psql) {
    Write-Error "psql.exe not found. Please verify PostgreSQL is installed."
    exit 1
}

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content $LogFile $line
    Write-Host $line
}

function RunPsql($h, $p, $u, $pw, $db, $sql) {
    $env:PGPASSWORD = $pw
    $result = & $psql -h $h -p $p -U $u -d $db -t -A -c $sql 2>$null
    return $result
}

function LoadState() {
    if (Test-Path $StateFile) {
        $json = Get-Content $StateFile -Raw | ConvertFrom-Json
        $ht = @{}
        foreach ($prop in $json.PSObject.Properties) {
            $ht[$prop.Name] = @{ last_id = [int]$prop.Value.last_id }
        }
        return $ht
    }
    return $null
}

function SaveState($state) {
    $state | ConvertTo-Json -Depth 3 | Set-Content $StateFile
}

function InitStateFromRemote() {
    Log "First run - initialising sync position from Raspberry Pi..."
    $state = @{}
    foreach ($tbl in $tables) {
        $maxId = RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB `
            "SELECT COALESCE(MAX(id), 0) FROM public.$tbl"
        $maxId = ($maxId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if (-not $maxId) { $maxId = 0 }
        $state[$tbl] = @{ last_id = [int]$maxId }
        Log "  $tbl : will sync from id > $maxId"
    }
    return $state
}

# --- Install as scheduled task ---
if ($Install) {
    $script   = $PSCommandPath
    $action   = New-ScheduledTaskAction `
                    -Execute "powershell.exe" `
                    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
                    -WorkingDirectory $PSScriptRoot
    $trigger  = New-ScheduledTaskTrigger `
                    -Once `
                    -At (Get-Date) `
                    -RepetitionInterval (New-TimeSpan -Minutes 1)
    $settings = New-ScheduledTaskSettingsSet `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
                    -MultipleInstances IgnoreNew
    Unregister-ScheduledTask -TaskName "BlueForsSync" -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName "BlueForsSync" `
        -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
    Write-Host "Scheduled task 'BlueForsSync' installed - runs every minute." -ForegroundColor Green
    exit 0
}

# --- Main sync ---
Log "=== Sync started ==="

$test = RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "SELECT 1"
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Cannot connect to Raspberry Pi $RemoteHost"
    exit 1
}
Log "Connected to Raspberry Pi OK"

$env:PGPASSWORD = $LocalPass
$localTest = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB -t -A `
    -c "SELECT COUNT(*) FROM double_value_change_events;" 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Cannot connect to local CS2 database - $localTest"
    exit 1
}
Log "Local CS2 database OK - $localTest rows in double_value_change_events"

$state = LoadState
if ($null -eq $state -or $state.Count -eq 0) {
    $state = InitStateFromRemote
    SaveState $state
}

$total = 0

foreach ($tbl in $tables) {
    $lastId = 0
    if ($state.ContainsKey($tbl)) { $lastId = $state[$tbl].last_id }

    $env:PGPASSWORD = $LocalPass
    $copyOut = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
        -c "\copy (SELECT * FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) TO stdout WITH CSV" `
        2>$null

    $lines = @($copyOut | Where-Object { $_ -and $_ -ne "" })
    if ($lines.Count -eq 0) { continue }

    $env:PGPASSWORD = $RemotePass
    $importErr = $lines | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
        -c "\copy public.$tbl FROM stdin WITH CSV" 2>&1

    if ($LASTEXITCODE -eq 0) {
        $newId = RunPsql $LocalHost $LocalPort $LocalUser $LocalPass $LocalDB `
            "SELECT id FROM (SELECT id FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) t ORDER BY id DESC LIMIT 1"
        $newId = ($newId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if ($newId) {
            $state[$tbl] = @{ last_id = [int]$newId }
            $total += $lines.Count
            Log "  $tbl : +$($lines.Count) rows (last id: $newId)"
        }
    } else {
        Log "  $tbl : import failed - $importErr"
    }
}

# device_states: full refresh (no id column — always overwrite)
$env:PGPASSWORD = $LocalPass
$dsData = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
    -c "\copy (SELECT * FROM public.device_states) TO stdout WITH CSV" 2>$null
$dsLines = @($dsData | Where-Object { $_ -and $_ -ne "" })
if ($dsLines.Count -gt 0) {
    $env:PGPASSWORD = $RemotePass
    RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "TRUNCATE public.device_states" | Out-Null
    $dsLines | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
        -c "\copy public.device_states FROM stdin WITH CSV" 2>$null | Out-Null
    Log "  device_states: updated $($dsLines.Count) devices"
}

SaveState $state
Log "=== Sync done: +$total rows total ==="
```

### How it works — step by step

| Step | What happens |
|---|---|
| 1 | Script starts, locates `psql.exe` in standard PostgreSQL install paths |
| 2 | Connects to Raspberry Pi PostgreSQL (172.31.255.62:5432) — exits if unreachable |
| 3 | Connects to local CS2 PostgreSQL (localhost:5434) — exits if unreachable |
| 4 | Loads `win_sync_state.json` which records the last synced row ID per table |
| 5 | **First run only:** queries the Pi for `MAX(id)` of each table; starts syncing from there (skips backup data already on the Pi) |
| 6 | For each table: exports up to 5000 new rows as CSV via `\copy … TO stdout`, then pipes them into the Pi via `\copy … FROM stdin` |
| 7 | `device_states` has no `id` column — truncates and re-imports the full table every run |
| 8 | Saves updated state to `win_sync_state.json` |

### Run it manually (test)

```powershell
powershell -ExecutionPolicy Bypass -File C:\bluefors_monitor\sync_push.ps1
```

Expected output on first run:

```
2026-06-18 19:33:49 === Sync started ===
2026-06-18 19:33:49 Connected to Raspberry Pi OK
2026-06-18 19:33:49 Local CS2 database OK - 4484137 rows in double_value_change_events
2026-06-18 19:33:49 First run - initialising sync position from Raspberry Pi...
2026-06-18 19:33:49   double_value_change_events : will sync from id > 3649339
2026-06-18 19:33:49   int_value_change_events : will sync from id > 56375
...
2026-06-18 19:33:53   device_states: updated 50 devices
2026-06-18 19:33:53 === Sync done: +27178 rows total ===
```

### Install as a Windows Scheduled Task

Run PowerShell **as Administrator** (right-click → Run as administrator):

```powershell
powershell -ExecutionPolicy Bypass -File C:\bluefors_monitor\sync_push.ps1 -Install
```

This creates a task named `BlueForsSync` that runs every minute with no time limit.

Verify it is running:

```powershell
Get-ScheduledTask -TaskName "BlueForsSync" | Select-Object TaskName, State
# State should be: Ready
```

View the log:

```powershell
Get-Content C:\bluefors_monitor\win_sync.log -Tail 20
```

---

## Monitor Script — monitor.py

Runs every minute on the Raspberry Pi via cron. Checks the local database for anomalies and forwards alerts to Slack.

### Full code

See [`monitor.py`](monitor.py) in this repository.

### How it works — step by step

Each run does the following in order:

| Step | Function | What it does |
|---|---|---|
| 1 | `update_mode()` | Reads `50K_TEMPERATURE`, determines current mode (IDLE / TRANSITIONING / COLD). If the mode changed since the last run, sends a Slack notification and clears all sensor cooldowns so the new mode's thresholds apply immediately |
| 2 | `check_acknowledgements()` | Polls Slack for reactions (✅ 👏 👍 🤙) or thread replies (`ok`/`OK`) on previous alert messages; silences that sensor for 10 min if found |
| 3 | `check_commands()` | Reads new `@BlueFors-Alert` mentions in the channel; parses and executes threshold-change, ack, mode-switch, or `pressure reading` commands |
| 4 | `check_data_freshness()` | Queries `MAX(time)` from `double_value_change_events`; fires an alert if data is more than 5 minutes old (with 30-min cooldown to avoid spam) |
| 5 | `check_sensor_thresholds()` | Selects the correct threshold set based on mode: IDLE → `THRESHOLDS_IDLE`, COLD → `THRESHOLDS_COLD`, TRANSITIONING → skips entirely. For each sensor, fetches the latest value and sends an alert if the limit is exceeded and cooldown has passed |
| 6 | `check_cs2_alerts()` | Fetches new rows from the `alerts` table with `severity >= CS2_ALERT_MIN_SEVERITY`; batches by error code and forwards to Slack |
| 7 | `check_r1a_status()` | Checks `boolean_value_change_events` for R1A pump enabled/error status changes, and `double_value_change_events` for R1A power crossing zero (pump on/off); sends a Slack alert for any change |
| 8 | Send + track | Sends all queued Slack messages; saves each message's `ts` (timestamp) so reactions on it can be checked next run |
| 9 | `save_state()` | Writes `monitor_state.json` — persists mode, last alert times, last CS2 alert ID, R1A event IDs, acknowledgements, threshold overrides |

### State file (monitor_state.json)

```json
{
  "current_mode": "COLD",
  "mode_since": "2026-06-18T10:00:00",
  "mode_override": null,
  "last_alert_time": {
    "MXC_TEMPERATURE": "2026-06-18T14:23:00"
  },
  "last_cs2_alert_id": 1156,
  "acked_sensors": {
    "MXC_TEMPERATURE": "2026-06-18T14:33:00"
  },
  "pending_alert_msgs": {
    "MXC_TEMPERATURE": { "ts": "1781830413.557959", "channel": "C0B42G4AU0N" }
  },
  "threshold_overrides_cold": {
    "MXC_TEMPERATURE": { "max_val": 0.05, "min_val": null, "expires_at": "2026-06-18T14:33:00" }
  },
  "threshold_overrides_idle": {},
  "last_slack_ts": "1781830413.557959",
  "last_freshness_alert": "2026-06-18T14:00:00",
  "ctx": {
    "sensor_key": "P2",
    "intent": "plot",
    "minutes": 720,
    "ts": 1782000000.0
  },
  "nlp_pending": []
}
```

Key fields:

| Field | Description |
|---|---|
| `current_mode` | Active mode: `"IDLE"`, `"TRANSITIONING"`, or `"COLD"` |
| `mode_since` | When the current mode started |
| `mode_override` | `null` = auto; `"IDLE"` or `"COLD"` = manually locked via Slack |
| `last_alert_time` | Timestamp of last alert per sensor (controls 30-min cooldown) |
| `acked_sensors` | Sensors silenced until this time (via Slack reaction or `ok` reply) |
| `pending_alert_msgs` | Slack `ts` of each outstanding alert (needed to check for reactions) |
| `threshold_overrides_cold` | Threshold overrides for COLD mode (keyed by sensor mapping) |
| `threshold_overrides_idle` | Threshold overrides for IDLE mode (separate from COLD) |
| `ctx` | Last-used sensor/intent for conversation context (expires after 10 min) |
| `nlp_pending` | NLP responses awaiting user feedback (self-learning loop) |

---

## Configuration — config.py

Located at `/home/cdms/bluefors_monitor/config.py` on the Raspberry Pi.  
The version in this repository has credentials replaced with placeholders (`YOUR_SLACK_BOT_TOKEN`, etc.). After cloning, fill in the real values before running the monitor.

```python
# BlueFors Monitor Configuration

# ── Local PostgreSQL (Raspberry Pi) ───────────────────────────────────────────
LOCAL_PG_HOST     = "localhost"
LOCAL_PG_PORT     = 5432
LOCAL_PG_USER     = "postgres"
LOCAL_PG_PASSWORD = "cs2monitor"
LOCAL_PG_DB       = "cs2"

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN   = "xoxb-..."          # from api.slack.com/apps → OAuth & Permissions
SLACK_CHANNEL     = "C0B42G4AU0N"       # channel ID (not name)
SLACK_BOT_USER_ID = "U0BBGRB0HC4"       # bot user ID — used to detect @mentions

# ── Operating mode detection ───────────────────────────────────────────────────
# 50K_TEMPERATURE is used to decide which threshold set to apply:
#   > MODE_IDLE_ABOVE_K    →  IDLE        (room temperature, fridge not running)
#   < MODE_COLD_BELOW_K    →  COLD        (operational)
#   between the two        →  TRANSITIONING (cooling/warming — threshold alerts suppressed)
MODE_DETECTION_SENSOR = "50K_TEMPERATURE"
MODE_IDLE_ABOVE_K     = 200.0
MODE_COLD_BELOW_K     = 80.0

# ── Thresholds: IDLE mode ──────────────────────────────────────────────────────
# Only pressures monitored — temperatures near room temperature are expected.
THRESHOLDS_IDLE = {
    # sensor mapping    : (max_value, min_value, description)
    "P2_PRESSURE":  (10.0,  None, "P2 pressure unusually high at room temperature"),
    "P5_PRESSURE":  (0.1,   None, "P5 pressure unusually high at room temperature"),
}

# ── Thresholds: COLD mode ──────────────────────────────────────────────────────
# Applied when 50K_TEMPERATURE < 80 K (fridge is cold and operational).
THRESHOLDS_COLD = {
    # sensor mapping          : (max_value, min_value, description)
    "MXC_TEMPERATURE":     (0.030,  None,  "MXC temperature > 30 mK"),
    "MXC_TEMPERATURE_FAR": (0.050,  None,  "MXC far-end temperature > 50 mK"),
    "STILL_TEMPERATURE":   (2.0,    None,  "Still temperature > 2 K"),
    "4K_TEMPERATURE":      (6.0,    None,  "4K plate > 6 K"),
    "50K_TEMPERATURE":     (65.0,   None,  "50K plate > 65 K"),
    "B1A_TEMPERATURE":     (1.0,    None,  "B1A stage > 1 K"),
    "B2_TEMPERATURE":      (4.5,    None,  "B2 stage > 4.5 K"),
    "P1_PRESSURE":         (20.0,   None,  "P1 return pressure > 20 mbar"),
    "P2_PRESSURE":         (0.5,    None,  "P2 still pressure > 0.5 mbar"),
    "P5_PRESSURE":         (1e-3,   None,  "P5 MXC pressure > 1e-3 mbar"),
    "FLOW_VALUE":          (None,   0.01,  "He flow < 0.01 mmol/s"),
}

# ── Alert behaviour ────────────────────────────────────────────────────────────
ALERT_COOLDOWN_MINUTES = 30     # minutes before same sensor can alert again
CS2_ALERT_MIN_SEVERITY = 2      # 1 = warning, 2 = error only
SYNC_BATCH_SIZE        = 5000   # rows per table per sync cycle (Windows side)
```

**To change a threshold permanently:** edit this file and the next cron run picks it up automatically (no restart needed).  
**To change a threshold temporarily:** use the `change` Slack command (see [Slack Interaction](#slack-interaction)).

---

## Slack App Setup

### Create the bot

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it `BlueFors-Alert`, select your workspace

### Add permissions (Bot Token Scopes)

Go to **OAuth & Permissions** → **Bot Token Scopes** → **Add an OAuth Scope**:

| Scope | Why it's needed |
|---|---|
| `chat:write` | Send alert messages |
| `channels:history` | Read channel messages to detect `@BlueFors-Alert` commands |
| `reactions:read` | Check if someone reacted ✅ on an alert to acknowledge it |
| `files:write` | Upload plot PNG images to Slack |
| `conversations:history` | Read thread replies (used by self-learning NLP feedback loop) |

After adding scopes, click **Reinstall to Workspace** at the top of the page.

### Get the token and IDs

- **Bot Token** (`xoxb-...`): shown at the top of the OAuth & Permissions page after install
- **Channel ID**: right-click the channel in Slack → **Copy link** — the ID is the last segment (`C0B42...`)
- **Bot User ID**: call `https://slack.com/api/auth.test` with your token — the `user_id` field

Test the token:

```bash
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer xoxb-YOUR-TOKEN" | python3 -m json.tool
# "ok": true  means it works
```

### Invite the bot to your channel

In Slack, open the channel and type:

```
/invite @BlueFors-Alert
```

---

## Starting the System

### Step 1 — Windows: copy and test the sync script

```powershell
# Copy from Pi to Windows (run on Windows)
scp cdms@172.31.255.62:/home/cdms/bluefors_monitor/sync_push.ps1 C:\bluefors_monitor\sync_push.ps1

# Test one run
powershell -ExecutionPolicy Bypass -File C:\bluefors_monitor\sync_push.ps1
```

### Step 2 — Windows: install scheduled task (run as Administrator)

```powershell
powershell -ExecutionPolicy Bypass -File C:\bluefors_monitor\sync_push.ps1 -Install
```

### Step 3 — Pi: initialise monitor state (run once, skips historical alerts)

```bash
cd /home/cdms/bluefors_monitor
python3 monitor.py --init
```

Output:

```
Initialised: last_cs2_alert_id=1134, skipped alerts for 11 sensors
--init complete.
```

### Step 4 — Pi: install cron job

```bash
bash setup_cron.sh
```

This writes the following line to crontab:

```
* * * * * python3 /home/cdms/bluefors_monitor/monitor.py >> /home/cdms/bluefors_monitor/monitor.log 2>&1
```

Verify:

```bash
crontab -l
```

### Step 5 — Verify everything is running

Check sync is receiving data (row count should grow each minute):

```bash
PGPASSWORD=cs2monitor psql -h localhost -U postgres -d cs2 \
  -c "SELECT COUNT(*) FROM double_value_change_events;"
```

Check monitor log:

```bash
tail -f /home/cdms/bluefors_monitor/monitor.log
```

---

## Slack Interaction

All commands are sent by mentioning `@BlueFors-Alert` in the channel. The bot replies within ~5 seconds (fast responder polls Slack every 5 s).

> **v3.0+ upgrade:** The exact commands below are still fully supported. In addition, the bot now understands **natural language** in Chinese and English — you no longer need to follow the exact syntax. See [NLP Natural Language Interface](#nlp-natural-language-interface) for details and examples.

### Acknowledge an alert (silence 10 minutes)

React to any alert message with **✅ 👏 👍 🤙**, or reply `ok` / `OK` in the alert thread.

### Commands

#### Status & readings

| Command | Description |
|---|---|
| `help` | Show all available commands |
| `temperature reading` | Current temperatures — MXC1, MXC2, Still, 4K, 50K, B1A, B2. Shows K and °C; auto-converts to mK when cold |
| `pressure reading` | Latest P1–P7 pressures + Cold Cathode ON/OFF |
| `pump status` | B1A, B2 (turbo), R1A, R2 (scroll), COM compressor — on/off, speed, power, temp |
| `heater status` | Still/MXC heat switches and heaters — on/off and power |
| `pulse tube status` | Pulse-tube compressor — coolant-in/out, oil, helium temps (°C); high/low pressure (psi); motor current (A); running state + active faults |
| `plot coolant in 2h` · `plot oil temp 12h` · `plot high pressure 1d` · `plot motor current` | Plot recorded pulse-tube values (coolant in/out, oil, helium, high/low pressure, motor current). Logged to `pulsetube_readings` every minute (see `schema_pulsetube.sql`) |
| `valve status` | All 25 valves grouped by open/closed state with last-change timestamp |
| `list` | All sensors with numbers, short names, and current thresholds |
| `status` | Current mode (+ cool-down/warm-up direction), active threshold overrides, silenced sensors |
| `mode` | Current operating mode |
| `what is the alarm` | List **every** configured alarm and its exact trigger criteria, grouped by mode |
| `what is current alarm criteria` | Trigger criteria for the **current** mode only |
| `what alarms in cold mode` | Alarms for one mode (`idle` / `cold` / `transitioning`) |
| `filter status` | Days until the chilled-water filter is due for replacement |
| `I have changed the filter` | Confirm filter replaced — restarts the 3-week reminder timer |

`MC` is accepted everywhere as an alias for `MXC` (mixing chamber): `plot mc`, `change mc to 0.035`, etc.

#### Plots

| Command | Description |
|---|---|
| `plot <sensor>` | Plot last 30 min as image (sends PNG to Slack) |
| `plot <sensor> 2h` | Plot last N hours — also accepts `2.5h`, `recent 2 hours`, natural language |
| `plot <sensor> 30min` | Plot last N minutes |
| `plot <sensor> 30 days` | Longer ranges: `days`, `weeks`, `months`, `years` (e.g. `plot P1 P5 for past 30 days`, `3 weeks`, `6 months`, `1 year`; EN + 中文 天/周/月/年) |
| `plot <sensor> YYMMDD_HHMM YYMMDD_HHMM` | Plot a specific time range (CDT) |
| `plot <s1> <s2> 2h` | **Multi-sensor comparison** — overlay two or more sensors on one chart |
| `plot <s1> <s2> <s3> 12h` | Up to ~6 sensors; dual y-axis when units differ (e.g. mbar + K) |

Available sensors: `P1`–`P7`, `MXC`, `STILL`, `4K`, `50K`, `B1A`, `B2`, `FLOW`

Temperature plots auto-convert to mK when all values are below 1 K (e.g. MXC at base temperature).

**Context shortcuts** (work within 10 minutes of the previous command):

| Command | What it does |
|---|---|
| `longer` | Re-plot the last sensor at 4× the previous time range |
| `plot 12h` | Plot the last sensor at a new duration |
| `plot it` / `plot again` | Plot the last sensor again |

#### Acknowledgement & silencing

| Command | Description |
|---|---|
| `ack` | Silence ALL sensors for 10 minutes |
| *(reply in an alert thread)* `ok` / ✅ | Silence that one sensor for 10 minutes |
| *(reply in an alert thread)* `silent for 2h` | Silence that one sensor for any duration — `mute 30min`, `silence 3 days`, `silent forever` (min / hour / day, English or Chinese) |

#### Mode control

| Command | Description |
|---|---|
| `set mode auto` | Return to automatic mode detection (50K temperature) |
| `set mode idle` | Force IDLE mode (pressure checks only) |
| `set mode cold` | Force COLD mode (full threshold monitoring) |
| `set mode transitioning` | Force TRANSITIONING mode (cooling/warming — threshold alerts off, pulse-tube check on) |

#### Threshold changes (mode-specific)

Threshold overrides are **separate for COLD and IDLE modes** — changing one mode never affects the other.

| Command | Description |
|---|---|
| `change <sensor> to <value> for 30min` | Override threshold for **current** mode, expires after N min |
| `change <sensor> to <value> for 2h` | Override for current mode, expires after N hours |
| `change <sensor> to <value> for ever` | Override for current mode, permanent |
| `cold change <sensor> to <value> for ever` | Override COLD mode threshold explicitly |
| `idle change <sensor> to <value> for ever` | Override IDLE mode threshold explicitly |
| `reset <sensor>` | Restore default for current mode |
| `cold reset <sensor>` | Restore COLD mode default |
| `idle reset <sensor>` | Restore IDLE mode default |

Duration options: `30min`, `2h`, `24h`, `for ever` (any number of minutes or hours).  
The `<sensor>` field accepts a number, short name, or full mapping name.

#### Alerts toggle

| Command | Description |
|---|---|
| `sentinel on` | Resume CS2 system alert forwarding |
| `sentinel off` | Pause CS2 system alert forwarding |

### Valve state change alerts (V112 / V113 / V114)

The bot monitors **V112, V113, and V114** continuously and sends an automatic Slack alert whenever any of these valves opens or closes — active in **both COLD and IDLE modes**:

> :valve: V112 changed: **OPEN** → **CLOSED** _(at 2026-06-29 14:23)_

This is separate from `valve status` (which shows all 25 valves on demand). Alerts fire on every state change with no cooldown.

---

### Device Health Monitoring

Every device in the `device_states` table (≈50 devices: pulse-tube compressor, helium compressor, rough/turbo pumps, pressure gauges P1–P7, all valves, GHS, LEDs, …) is checked **in every mode**, every cron cycle, from a single query. Each device publishes its own health in its JSON snapshot, and the monitor raises:

- **CRITICAL** 🚨 — if the device reports an error: `statusInfo.errors` / `statusInfo.errorBit`, or any `bError…` flag is true.
- **WARNING** ⚠️ — if it reports a warning: `statusInfo.warnings` / `statusInfo.warningBit`, or any `bWarning…` flag is true.

Flag names are humanised (`bErrorOilRunningHigh` → "Oil Running High") and the device's friendly name (`instrumentInfo.name`) is used. Example:

> 🚨 **CRITICAL — Pulse tube 1: error** 🚨
> &nbsp;&nbsp;&nbsp;&nbsp;• Oil Running High

Behaviour:
- Re-alerts when the active-fault set changes or after the cooldown (`ALERT_COOLDOWN_MINUTES`); clears automatically when the device returns to healthy.
- Per-device + per-severity acknowledgement — reply `silent for 2h` in the thread to snooze just that fault.
- Benign, noisy gauge flags (pressure-gauge under/over-range) are ignored via `config.DEVICE_FLAG_IGNORE`.
- **Only-when-running:** a device's operational fault flags are evaluated only while it is running (`bCompressorRunning` / `bPumpOnOff`). The pulse-tube compressor's coolant/oil/helium/pressure/current limits therefore fire **only when the pulse tube is ON** — when it is intentionally off, those readings are ignored (device-level comms faults are still reported).
- New devices added by BlueFors are picked up automatically — no code changes needed.

This uses the **manufacturers' own factory limits** (e.g. Cryomech's built-in fault flags — no numeric thresholds are exposed in the data, so there is nothing to tune). Each of the pulse-tube parameters — coolant-in, coolant-out, oil, helium temperature; high and low pressure; motor current — has both a **CRITICAL** (`bError…`) and a **WARNING** (`bWarning…`) factory limit. The pulse-tube compressor JSON also exposes the raw analog diagnostics (temperatures in K; low/high pressure in Pa; motor current in A).

### Compressed-air pressure alerts (GHS)

The gas-handling-system compressed air is monitored in every mode from `device_states` (device `plc.GHSDiagnostics`), with two levels (thresholds stored in Pa, displayed in kPa; configurable in `config.AIR_PRESSURE_ALARMS`):

| Line | Field | Normal | Warning | Critical |
|---|---|---|---|---|
| Input air | `fInputAirPressure` | 690 kPa | < 620 kPa | < 540 kPa |
| Regulator air | `fRegulatorAirPressure` | 492 kPa | — | < 485 kPa |

Escalation (warning → critical) fires immediately, bypassing the cooldown; recovery clears the state.

### Chilled-water filter reminder

Counting from first run, every **3 weeks** (`FILTER_INTERVAL_DAYS`) the bot posts a replacement reminder and then repeats it every **10 minutes** (all in one thread) until someone replies that it was changed — e.g. `I have changed it`, `filter replaced`, `已经更换`. Note: `ok` / `yes` deliberately do **not** stop it. Confirmation restarts the timer. This reminder runs independently of `pause alerts` / `ack`. Check remaining time any time with `filter status`.

---

### Daily summary

The bot automatically sends a 12-hour summary to Slack at **8:00 AM** and **8:00 PM CDT** covering:
- Current mode and all sensor readings (temperatures, pressures, flow, Pulse Tube)
- Device state changes (pumps, heaters, heat switches, cold cathode)
- CS2 alerts fired
- Linear trend direction (📈 📉 ➡️) for each sensor over the 12-hour window

### Sensor identifiers

#### COLD mode sensors

| # | Short name | Full mapping name | Unit | Default alert condition |
|---|---|---|---|---|
| 1 | MXC | MXC_TEMPERATURE | K | > 0.030 K |
| 2 | MXCFAR | MXC_TEMPERATURE_FAR | K | > 0.050 K |
| 3 | STILL | STILL_TEMPERATURE | K | > 2.0 K |
| 4 | 4K | 4K_TEMPERATURE | K | > 6.0 K |
| 5 | 50K | 50K_TEMPERATURE | K | > 65.0 K |
| 6 | B1A | B1A_TEMPERATURE | K | > 1.0 K |
| 7 | B2 | B2_TEMPERATURE | K | > 4.5 K |
| 8 | P1 | P1_PRESSURE | bar | > 0.02 bar (20 mbar) |
| 9 | P2 | P2_PRESSURE | bar | > 5e-4 bar (0.5 mbar) |
| 10 | P5 | P5_PRESSURE | bar | > 1e-6 bar (1e-3 mbar) |
| 11 | FLOW | FLOW_VALUE | mmol/s | < 0.01 mmol/s |

#### IDLE mode sensors

| # | Short name | Full mapping name | Unit | Default alert condition |
|---|---|---|---|---|
| 9 | P2 | P2_PRESSURE | bar | > 0.01 bar (10 mbar) |
| 10 | P5 | P5_PRESSURE | bar | > 1e-4 bar (0.1 mbar) |

### Examples

```
@BlueFors-Alert temperature reading
@BlueFors-Alert pressure reading
@BlueFors-Alert pump status
@BlueFors-Alert heater status
@BlueFors-Alert valve status
@BlueFors-Alert plot P2
@BlueFors-Alert plot MXC 12h
@BlueFors-Alert plot P2 P5 2h
@BlueFors-Alert plot MXC STILL 4K 12h
@BlueFors-Alert plot P1 260620_0000 260622_1200
@BlueFors-Alert longer
@BlueFors-Alert cold change MXC to 0.035 for ever
@BlueFors-Alert idle change P2 to 0.008 for 24h
@BlueFors-Alert cold reset MXC
@BlueFors-Alert sentinel off
@BlueFors-Alert set mode cold
@BlueFors-Alert ack
```

---

## NLP Natural Language Interface

Starting from **v3.0.0**, the bot understands natural language **on top of** all the exact commands listed in [Slack Interaction](#slack-interaction). The exact-match commands (`pressure reading`, `plot P2 12h`, `cold change MXC to 0.035 for ever`, etc.) continue to work exactly as before — they are parsed first with zero overhead. The NLP layer activates only when the message does not match any exact pattern, so it adds capability without breaking anything.

### How it works

The classifier is a **TF-IDF + Logistic Regression** pipeline trained on ~200 bilingual (Chinese/English) example phrases. It runs entirely on the Raspberry Pi with no API calls and classifies each message in under 10 ms.

14 intents are supported: `plot`, `pressure reading`, `pump status`, `heater status`, `valve status`, `change threshold`, `reset threshold`, `sentinel`, `set mode`, `acknowledge`, `daily summary`, `help`, `status`, and device-specific routing (see below).

### Natural language examples

```
@BlueFors-Alert how is P2 pressure doing in the last hour
@BlueFors-Alert MXC temperature is too high, raise the threshold to 35 mK
@BlueFors-Alert what is the pump doing
@BlueFors-Alert turn off alerts
@BlueFors-Alert show me a plot of P5 for 12 hours
@BlueFors-Alert set cold mode P2 threshold to 5e-4
@BlueFors-Alert summarize the last 12 hours
@BlueFors-Alert comparison plot of P2 and P5 for 2 hours
@BlueFors-Alert what's the pump doing
@BlueFors-Alert show me the heater status
@BlueFors-Alert plot P2 for the last two hours
```

#### Device-specific queries (v3.3.0)

The bot understands natural language queries about **specific pumps and valves by name**:

```
@BlueFors-Alert what is the status of R2
@BlueFors-Alert how is R2 doing
@BlueFors-Alert is R1A running
@BlueFors-Alert B1A status
@BlueFors-Alert COM on or off
@BlueFors-Alert what is V113 doing
@BlueFors-Alert is V112 open
@BlueFors-Alert V106 status
```

When a device name is detected (R2, R1A, B1A, COM, V112, V106, …), the bot routes to the correct command (`pump status` or `valve status`) and highlights the queried device at the top of the reply with a `◄` marker. All other devices are still shown for context.

### Confidence hint and self-learning (v3.1.0)

When the NLP classifier is uncertain (confidence < 45%), the bot appends:

> *(Auto-detected as: pump status. Reply "yes" to confirm or "wrong" if incorrect — I'll learn from it.)*

**To correct a misclassification**, reply in the same Slack thread:

```
wrong, I meant heater status
```

The bot:
1. Saves the corrected example to `nlp_user_examples.jsonl`
2. Rebuilds the classifier immediately (< 0.5 s, on-device)
3. Confirms: "✅ Got it — I'll treat that as *heater status* next time."

To **confirm** a correct detection, reply `yes` or `correct` — this also adds the phrase as a positive training example, improving accuracy over time.

All learned examples persist across restarts. The system becomes more accurate with regular use.

### Conversation context (v3.2.0)

The bot remembers the last sensor and time range for **10 minutes**:

```
@BlueFors-Alert plot P2 12h          ← remembered
@BlueFors-Alert longer               ← extends to 48h, same sensor
@BlueFors-Alert plot it again        ← P2 again from context
@BlueFors-Alert plot 2h              ← "plot" without sensor → uses P2
```

Context is stored in `monitor_state.json` and resets after 10 minutes of inactivity.

---

## Database Schema

### double_value_change_events

Every numerical sensor reading from the CS2 system.

```sql
id       BIGINT PRIMARY KEY
time     TIMESTAMPTZ          -- when the value was recorded
mapping  VARCHAR              -- sensor name, e.g. "MXC_TEMPERATURE"
value    DOUBLE PRECISION     -- the reading
value_id VARCHAR
```

Query latest value per sensor:

```sql
SELECT DISTINCT ON (mapping) mapping, value, time
FROM double_value_change_events
ORDER BY mapping, time DESC;
```

### alerts

CS2 system alerts generated by the control software.

```sql
id                  BIGINT PRIMARY KEY
code                INTEGER
datetime            TIMESTAMPTZ
title               VARCHAR
description         VARCHAR
severity            INTEGER      -- 1 = warning, 2 = error
originator          VARCHAR
resolution_datetime TIMESTAMPTZ
resolved_by         VARCHAR
```

### device_states

Current state snapshot of all 50 connected devices. Fully replaced every sync cycle.

```sql
datetime   TIMESTAMPTZ
device_id  VARCHAR PRIMARY KEY
values     JSONB                -- device-specific state fields
```

---

## Sensor Mappings

All sensor names found in `double_value_change_events`:

| Mapping | Unit | Description |
|---|---|---|
| `MXC_TEMPERATURE` | K | Mixing chamber temperature |
| `MXC_TEMPERATURE_FAR` | K | Mixing chamber far-end temperature |
| `STILL_TEMPERATURE` | K | Still temperature |
| `4K_TEMPERATURE` | K | 4K plate temperature |
| `50K_TEMPERATURE` | K | 50K plate temperature |
| `B1A_TEMPERATURE` | K | B1A stage temperature |
| `B2_TEMPERATURE` | K | B2 stage temperature |
| `P1_PRESSURE` | mbar | Return line pressure |
| `P2_PRESSURE` | mbar | Still pressure |
| `P3_PRESSURE` | mbar | Condenser pressure |
| `P4_PRESSURE` | mbar | Pumping line pressure |
| `P5_PRESSURE` | mbar | MXC pressure |
| `P6_PRESSURE` | mbar | Backing pressure |
| `P7_PRESSURE` | mbar | Foreline pressure |
| `FLOW_VALUE` | mmol/s | Helium circulation flow rate |
| `HELIUM_TANK_VALUE` | — | Helium tank level |
| `MXC_HEATING_POWER` | W | MXC heater power |
| `STILL_HEATING_POWER` | W | Still heater power |
| `MXC_TARGET_TEMPERATURE` | K | MXC temperature setpoint |
| `STILL_TARGET_TEMPERATURE` | K | Still temperature setpoint |
| `COM_PUMP_POWER` | W | Compressor pump power |
| `R1A_PUMP_POWER` | W | R1A pump power |
| `R2_PUMP_POWER` | W | R2 pump power |

---

## Troubleshooting

### Sync: cannot connect to Raspberry Pi

```powershell
# Test connectivity from Windows
ping 172.31.255.62
Test-NetConnection -ComputerName 172.31.255.62 -Port 5432
```

If port 5432 is blocked, check UFW on the Pi:

```bash
sudo ufw status
sudo ufw allow from 172.31.255.0/16 to any port 5432
```

### Sync: duplicate key error

Happens when `win_sync_state.json` is missing or empty and the script tries to re-send rows already in the Pi database. Fix:

```powershell
# Delete state file — next run re-initialises from Pi's max IDs
Remove-Item C:\bluefors_monitor\win_sync_state.json -ErrorAction SilentlyContinue
powershell -ExecutionPolicy Bypass -File C:\bluefors_monitor\sync_push.ps1
```

### Sync: 0 rows pushed every run

The state file was initialised to the Pi's current max IDs. If the Pi is already up to date, 0 rows is correct. To check:

```bash
# On Pi: how old is the latest data?
PGPASSWORD=cs2monitor psql -h localhost -U postgres -d cs2 \
  -c "SELECT MAX(time) FROM double_value_change_events;"
```

### Scheduled task stops / Access Denied on install

The task must be registered as Administrator. Right-click PowerShell → **Run as administrator**, then run the `-Install` command again.

```powershell
# Check task status
Get-ScheduledTask -TaskName "BlueForsSync" | Select-Object TaskName, State, LastRunTime
```

### Monitor: Slack messages not arriving

Check the log:

```bash
tail -50 /home/cdms/bluefors_monitor/monitor.log
```

Test the bot token:

```bash
curl -s https://slack.com/api/auth.test \
  -H "Authorization: Bearer xoxb-YOUR-TOKEN" | python3 -m json.tool
```

Confirm the bot is in the channel (type `/invite @BlueFors-Alert` in Slack if not).

Check required scopes are granted (`channels:history`, `reactions:read`, `chat:write`):

```bash
curl -s "https://slack.com/api/conversations.history?channel=C0B42G4AU0N&limit=1" \
  -H "Authorization: Bearer xoxb-YOUR-TOKEN" | python3 -m json.tool
# "ok": true confirms channels:history is granted
```

### Re-initialise monitor state (clear all cooldowns and overrides)

```bash
cd /home/cdms/bluefors_monitor
python3 monitor.py --init
```

### Monitor: wrong mode detected / stuck in wrong mode

Check the current mode and the raw 50K sensor value:

```bash
# What mode does the state file say?
python3 -c "import json; s=json.load(open('monitor_state.json')); print(s.get('current_mode'), s.get('mode_override'))"

# What is the latest 50K plate temperature?
PGPASSWORD=cs2monitor psql -h localhost -U postgres -d cs2 \
  -c "SELECT value, time FROM double_value_change_events WHERE mapping='50K_TEMPERATURE' ORDER BY time DESC LIMIT 1;"
```

If the sensor reading looks wrong, override the mode manually via Slack:

```
@BlueFors-Alert set mode cold      # force COLD mode
@BlueFors-Alert set mode idle      # force IDLE mode
@BlueFors-Alert set mode auto      # restore auto-detection when sensor recovers
```

### Monitor: too many alerts / false positives during cool-down

During cool-down the 50K temperature should pass through the TRANSITIONING band (80–200 K) and threshold alerts should be automatically suppressed. If you are receiving alerts during cool-down, check:

1. Is the system currently in TRANSITIONING mode?

   ```bash
   python3 -c "import json; print(json.load(open('monitor_state.json')).get('current_mode'))"
   ```

2. If it shows IDLE or COLD, the 50K sensor may be reading incorrectly. Override mode:

   ```
   @BlueFors-Alert set mode auto
   ```

   Or if the sensor is broken, force COLD or IDLE to suppress the wrong threshold set.

---

## File Reference

| File | Location | Description |
|---|---|---|
| `sync_push.ps1` | Windows `C:\bluefors_monitor\` | Pushes CS2 data to Pi every minute |
| `win_sync_state.json` | Windows `C:\bluefors_monitor\` | Tracks last synced row ID per table (auto-generated) |
| `win_sync.log` | Windows `C:\bluefors_monitor\` | Sync run log (auto-generated) |
| `config.py` | Pi `/home/cdms/bluefors_monitor/` | Threshold settings, air-pressure & filter config, device-flag ignore list. Imports secrets from `config_secret.py` |
| `config_secret.py` | Pi `/home/cdms/bluefors_monitor/` | **Local only, git-ignored** — DB password and Slack bot token |
| `monitor.py` | Pi `/home/cdms/bluefors_monitor/` | Alert monitor + Slack command handler (cron every minute; threshold/device-health/air-pressure/filter checks; NLP dispatch) |
| `slack_responder.py` | Pi `/home/cdms/bluefors_monitor/` | Fast Slack responder — polls every 5 s for instant command replies; runs as background process |
| `nlp_intent.py` | Pi `/home/cdms/bluefors_monitor/` | Local NLP classifier (TF-IDF + Logistic Regression); bilingual intent classification and entity extraction |
| `nlp_user_examples.jsonl` | Pi `/home/cdms/bluefors_monitor/` | User-corrected training examples accumulated via self-learning (auto-generated; do not delete) |
| `setup_cron.sh` | Pi `/home/cdms/bluefors_monitor/` | Installs the cron job |
| `monitor_state.json` | Pi `/home/cdms/bluefors_monitor/` | Runtime state — alert times, acks, overrides, NLP context (auto-generated) |
| `monitor.log` | Pi `/home/cdms/bluefors_monitor/` | Monitor run log (auto-generated) |
| `responder.log` | Pi `/home/cdms/bluefors_monitor/` | Slack responder log (auto-generated) |

---

## GitHub

Source: [https://github.com/ZhihengLi0/column_monitor](https://github.com/ZhihengLi0/column_monitor)

Also included as a submodule in [SuperCDMS](https://github.com/ZhihengLi0/SuperCDMS).

> `config.py` is included in the repository with all credentials replaced by placeholders.  
> After cloning, open it on the Pi and fill in the real Slack token and database password before starting the monitor.

---

## Version History

Each version is tagged in the git repository. Use `git checkout <tag>` to inspect the code at any point.

### v1.x — Single-mode monitor (fridge assumed always cold)

| Version | Tag | Description |
|---------|-----|-------------|
| 1.0.0 | [v1.0.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v1.0.0) | Initial release — `sync_push.ps1` pushes Windows CS2 data to Pi every minute; `monitor.py` alerts on COLD sensor thresholds via Slack |
| 1.1.0 | [v1.1.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v1.1.0) | Sync stabilization — fixed PostgreSQL port (5434), Windows Scheduled Task repeat bug, sparse ID query bug, state init from Pi max IDs |
| 1.2.0 | [v1.2.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v1.2.0) | Interactive Slack — emoji reactions to acknowledge alerts, `ack`, `change <sensor>`, `reset <sensor>` commands |
| 1.3.0 | [v1.3.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v1.3.0) | Full documentation — comprehensive README with architecture, setup guide, database schema reference, troubleshooting |

### v2.x — Dual-mode monitor (IDLE + COLD operating modes)

| Version | Tag | Description |
|---------|-----|-------------|
| 2.0.0 | [v2.0.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.0.0) | **Dual-mode system** — IDLE (room temperature, pressure checks only), TRANSITIONING (alerts suppressed), COLD (full monitoring). Auto-detected from 50K plate temperature. Slack mode commands added |
| 2.1.0 | [v2.1.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.1.0) | Bug fixes — `set mode` not clearing acked sensors, `reset` showing wrong default, duplicate log entries, Windows Scheduled Task failing silently (fixed with `schtasks /ru SYSTEM`) |
| 2.2.0 | [v2.2.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.2.0) | Data catch-up — batch size tooling for recovering 1.8M+ rows after sync outage; restored to 5000 rows/min for normal operation |
| 2.3.0 | [v2.3.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.3.0) | Reliability — fixed silent monitor crash-loop caused by empty state file (process killed mid-write); atomic state file writes using `.tmp` rename |
| 2.4.0 | [v2.4.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.4.0) | Pressure reading command — `@BlueFors-Alert pressure reading` returns P1–P7 instantly; fixed Slack API timestamp precision bug (commands silently dropped); pressure units corrected (database stores bar, display converts to mbar/μbar) |
| 2.5.0 | [v2.5.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.5.0) | Fast responder — `slack_responder.py` polls Slack every 5 seconds for instant command replies (down from up to 1 minute); `sentinel on/off` command to pause/resume CS2 alert forwarding |
| 2.6.0 | [v2.6.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.6.0) | Status commands and device alerts — `pump status` (B1A, B2, R1A, R2, COM), `heater status` (Still/MXC switches and heaters); automatic Slack alerts on R1A pump, heater, and Pulse Tube state changes |
| 2.7.0 | [v2.7.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v2.7.0) | Cold cathode monitoring — P1 (Pfeiffer MPT200) on/off shown in `pressure reading`; alert if cold cathode is ON during IDLE mode or OFF during COLD mode |

### v3.x — Natural Language Interface & Self-Learning (major milestone)

This release series marks a fundamental shift: the bot moves from rigid command formats to **conversational natural language understanding**. Anyone can now interact in plain English or Chinese without memorising exact syntax.

| Version | Tag | Description |
|---------|-----|-------------|
| 3.0.0 | [v3.0.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.0.0) | **Natural language understanding** — `nlp_intent.py` adds a local TF-IDF + Logistic Regression classifier. Supports 12 intents in Chinese and English via character n-grams; no API cost, runs on-device in < 10 ms. All existing exact-match commands continue to work. Entity extraction: sensor names, durations, time ranges, numeric values with unit conversion (mK→K, mbar→bar), cold/idle mode prefix, on/off |
| 3.1.0 | [v3.1.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.1.0) | **Self-learning from Slack feedback** — when NLP confidence < 45%, the bot appends a correction invite in thread. User replies `"wrong, I meant pump status"` → example saved to `nlp_user_examples.jsonl` → classifier rebuilt immediately. Confirmed correct responses (`yes`/`correct`) also add positive training examples. Accuracy improves organically through normal use with no manual retraining |
| 3.2.0 | [v3.2.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.2.0) | **Multi-sensor comparison plots + conversation context** — `plot P2 P5 2h` overlays multiple sensors on one chart; dual y-axis when units differ (mbar vs K). Conversation context window (10 min): `longer` extends the last plot, `plot 2h` reuses the last sensor, `plot it` resolves to last sensor |
| 3.3.0 | [v3.3.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.3.0) | **Valve status + device-specific NLP routing** — `valve status` command shows all 25 valves grouped open/closed with last-change timestamps; automatic Slack alerts on V112/V113/V114 state changes (active in both COLD and IDLE modes). NLP classifier upgraded to 14 intents and now understands device-specific queries: "what is the status of R2", "is V112 open" — routes to `pump status` or `valve status` and highlights the queried device first in the reply |
| 3.4.0 | [v3.4.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.4.0) | **Temperature reading + plot improvements** — `temperature reading` command shows MXC/Still/4K/50K/B1A/B2 with automatic mK conversion when cold. Temperature plots auto-convert y-axis to mK when all values < 1 K. Plot duration parser now accepts natural language ("recent 2 hours", "2.15h", "last 30 minutes"). NLP confirmation: replying `yes` to hint message saves a positive training example. Fixed daily summary cron (was firing at 1 AM/1 PM CDT instead of 8 AM/8 PM CDT) |
| 3.5.0 | [v3.5.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.5.0) | **Global alert pause/resume** — `pause alerts` stops all Slack notifications while background monitoring continues; `resume alerts` re-enables. NLP understands natural language ("stop all alerts", "mute alerts", "暂停报警"). Bot confirms the action immediately. Separated from `sentinel` (CS2-only toggle) |
| 3.6.0 | [v3.6.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.6.0) | **Flexible silencing, aliases & reliability** — reply `silent for 2h` / `mute 30min` / `silence 3 days` / `silent forever` in an alert thread to snooze just that sensor for any duration (min/hour/day, EN + 中文); `set mode transitioning` command; `MC` accepted as an alias for `MXC`; automatic alerts when the **B1A/B2 turbo pumps** turn on/off; `fcntl` file lock on the state file to prevent corruption and lost updates |
| 3.7.0 | [v3.7.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.7.0) | **Alarm criteria query + chilled-water filter reminder** — `what is the alarm` lists every configured alarm with its exact trigger criterion grouped by mode; `what alarms in cold mode` / `what is current alarm criteria` scope it. Plus a filter-replacement reminder: every 3 weeks, repeated every 10 min until someone replies "I have changed it" (`ok` does not count); `filter status` command; independent of pause/ack |
| 3.8.0 | [v3.8.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v3.8.0) | **TRANSITIONING pulse-tube critical check** — the Pulse Tube must stay ON while cooling/warming or a CRITICAL alert fires (state-based, repeats until fixed). Direction (cool down vs warm up) is auto-detected from the 50K-plate trend and shown in alerts / `status` / the alarm listing. Per-direction requirements configurable via `TRANSITIONING_REQUIRED` |

### v4.x — Whole-system device health monitoring (major milestone)

| Version | Tag | Description |
|---------|-----|-------------|
| 4.0.0 | [v4.0.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.0.0) | **Per-device health monitoring for every device** — a single generic check reads the `device_states` JSON for all ~50 devices and raises CRITICAL on any error flag (`statusInfo.errors`/`errorBit`/`bError*`) and WARNING on any warning flag (`statusInfo.warnings`/`warningBit`/`bWarning*`), using each manufacturer's own limits (pulse-tube & helium compressors, rough/turbo pumps, pressure gauges, valves, GHS, …). Flag names humanised; benign gauge under/over-range ignored; per-device+severity ack; new devices auto-covered. Adds **GHS compressed-air low-pressure alarms** (input < 620/540 kPa, regulator < 485 kPa) read from `device_states` in Pa. Discovered these diagnostics were already local (in `device_states`), so no extra sync was needed |
| 4.1.0 | [v4.1.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.1.0) | **Plot durations up to years** — the plot duration parser now accepts `days`, `weeks`, `months`, `years` (EN + 中文 天/周/月/年), e.g. `plot P1 P5 for past 30 days`. Fixes long durations being read as minutes; `month`/`mo` disambiguated from bare `m` |
| 4.2.0 | [v4.2.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.2.0) | **Running-gated device faults + explicit pulse-tube criteria** — operational fault flags are evaluated only while the device is running (`bCompressorRunning`/`bPumpOnOff`), so the pulse-tube compressor's coolant/oil/helium/pressure/current limits fire only while it is ON. The alarm listing now spells out the pulse-tube parameters and their factory CRITICAL/WARNING limits |
| 4.3.0 | [v4.3.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.3.0) | **`pulse tube status` command** — live compressor readings: coolant-in/out, oil, helium temperatures (°C), high/low pressure (psi), motor current (A), running state, total operating hours, and any active factory-limit faults. Triggered by `pulse tube status` / `pulsetube …` / `pt status` |
| 4.4.0 | [v4.4.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.4.0) | **Readout tweaks + cool-down milestone** — `temperature reading` now shows both MXC thermometers (MXC1, MXC2) and adds °C alongside K. New informational notification when a plate cools past a threshold on the way down (default: 4K plate < 10 K, one-time, re-arms after warming; config `COOLDOWN_MILESTONES`) |
| 4.6.0 | [v4.6.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.6.0) | **Alarm recovery notifications** — when a condition alarm returns to normal the bot now posts a green `✅ Cleared` message automatically (e.g. "data sync has caught up"), so it's clear whether an alarm is still active or resolved. Covers data freshness, sensor thresholds, air pressure, coolant-vs-dew-point, cold cathode, transitioning pulse-tube, and per-device health. Also: image-upload retries with a text fallback so a plot request never ends in silence |
| 4.6.1 | [v4.6.1](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.6.1) | **Cleared notifications in thread + channel** — the one-sentence `✅ Cleared` message is now posted both at channel level and as a reply in the original alarm's thread (the alarm's Slack ts is captured when it fires) |
| 4.8.0 | [v4.8.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.8.0) | **Per-sensor staleness alarm** — each continuously-sampled sensor's own latest timestamp is checked; if any single thermometer or pressure gauge stops updating (older than its limit) an alarm fires, even while the others keep updating. The old global freshness check used the whole-table max time, so one thermometer freezing went unnoticed. All modes, with recovery notifications |
| 4.8.1 | [v4.8.1](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.8.1) | **Data-driven staleness thresholds + confidence wording** — per-sensor limits set from each sensor's real 7-day gap distribution to avoid false alarms (main thermometers 5 min; 50K 10 min; pressures 30 min, P4 45; B1A/B2 6 h — these are value-change events, so stable values gap). Two alarm styles: steady thermometers get a definite ":sos: Sensor not updating"; sensors that naturally gap when stable get a softer ":grey_question: no update for a while — may be steady or offline, worth a look". Silencing an alarm also quiets its recovery message |
| 4.7.0 | [v4.7.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.7.0) | **Pulse-tube recording & plotting** — the compressor's coolant-in/out, oil, helium temperatures, high/low pressure and motor current are logged every minute to `pulsetube_readings` (device_states keeps only the latest snapshot). Plot them with `plot coolant in 2h`, `plot oil temp 12h`, `plot high pressure 1d`, `plot motor current`, etc. (temps in °C, pressures in psi) |
| 4.5.0 | [v4.5.0](https://github.com/ZhihengLi0/column_monitor/releases/tag/v4.5.0) | **LN2 integration, dew-point alarm & threshold fixes** — `@mention` queries about liquid nitrogen (`ln2`, `weight`, `液氮`, …) are routed to the separate [ln2_monitor](https://github.com/ZhihengLi0/ln2_monitor) project. New CRITICAL alarm (all modes): the pulse-tube coolant-in temperature must stay above the dew point (from the LN2 humidity sensor) or the cooling line condenses. Threshold corrections: B1A/B2 are turbo pumps at room temperature → 315 K overheat limit (were 1 K / 4.5 K); He flow alarms when > 0.002 mmol/s; P5 MXC > 0.01 bar; Regulator air critical 485 → 479 kPa |

---

## Appendix A — CS2 System & Database Background

### What is CS2?

CS2 (Control System 2) is the control and monitoring system for the **BlueFors XLD dilution refrigerator** used in the SuperCDMS dark matter experiment at the University of Minnesota. A dilution refrigerator is an ultra-low-temperature device capable of cooling experimental payloads to ~10 millikelvin (−273.14 °C), widely used in quantum computing and low-temperature physics research.

CS2 is responsible for:
- Real-time acquisition of all sensor data (temperature, pressure, flow rate, heater power, etc.)
- Control of actuators: valves, pumps, heaters
- Logging of system alerts and automated operation records (cool-down, warm-up sequences)

### Database Overview

| Item | Details |
|------|---------|
| Database engine | PostgreSQL 14.9 (on BlueFors Windows CS2 PC) |
| Backup size | ~2.1 GB |
| Data time range | 2026-05-21 to 2026-06-12 (initial backup) |
| Sensor value rows | ~12 million+ |
| Acquisition rate | 1 sample/second per device |
| Schema | `public` (PostgreSQL default) |

The database was migrated from the CS2 Windows PC to a 12.7 TB Linux server and subsequently mirrored to a Raspberry Pi for real-time monitoring. This repository implements the Pi-side sync and alert pipeline.

---

## Appendix B — Full Database Schema Reference

The CS2 database contains **13 core tables** across four functional groups.

```
CS2 Database (public schema)
│
├── [Sensor Data]
│   ├── double_value_change_events   → float readings (temp, pressure, power, flow)
│   ├── int_value_change_events      → integer values
│   ├── boolean_value_change_events  → on/off states (valves, relays)
│   ├── string_value_change_events   → string states
│   └── json_value_change_events     → complex JSON data
│
├── [Device State]
│   ├── device_states    → current state snapshot per device (overwritten each sync)
│   └── device_events    → full historical stream of device state changes
│
├── [Alerts & Logs]
│   ├── alerts           → CS2-generated errors and warnings
│   └── user_log_entries → manually entered operator log entries
│
└── [Automation & Control]
    ├── automation_events      → cool-down / warm-up procedure execution records
    ├── automation_state       → current running automation state
    ├── core_statemachine      → core CS2 state machine
    └── flyway_schema_history  → database schema migration history (managed by Flyway)
```

### `double_value_change_events` — Primary Sensor Table

The largest table (12 M+ rows). Every numerical sensor reading is appended here.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Auto-increment primary key |
| `time` | timestamptz | Acquisition timestamp |
| `mapping` | varchar(255) | Human-readable sensor name |
| `value` | double precision | Sensor reading |
| `value_id` | varchar(255) | Unique device path in the CS2 system |

**Key sensor mappings used by the monitor:**

| mapping | Unit | Description |
|---------|------|-------------|
| `MXC_TEMPERATURE` | K | Mixing chamber temperature (coldest point) |
| `MXC_TEMPERATURE_FAR` | K | Mixing chamber far-end temperature |
| `STILL_TEMPERATURE` | K | Still temperature |
| `4K_TEMPERATURE` | K | 4K cold plate temperature |
| `50K_TEMPERATURE` | K | 50K cold plate temperature |
| `B1A_TEMPERATURE` | K | B1A stage temperature |
| `B2_TEMPERATURE` | K | B2 stage temperature |
| `P1_PRESSURE`–`P7_PRESSURE` | mbar | Pressure sensors P1 through P7 |
| `FLOW_VALUE` | mmol/s | Helium circulation flow rate |
| `MXC_HEATING_POWER` | W | MXC heater power |
| `STILL_HEATING_POWER` | W | Still heater power |
| `COM_PUMP_POWER` | W | Compressor pump power |

Query latest value per sensor:

```sql
SELECT DISTINCT ON (mapping) mapping, value, time
FROM double_value_change_events
ORDER BY mapping, time DESC;
```

### `boolean_value_change_events` — Valve & Relay States

Records every open/close event for valves (V001–V503H), pumps, and relays.

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `time` | timestamptz | Time of state change |
| `mapping` | varchar | Device name |
| `value` | boolean | `true` = open/on, `false` = closed/off |
| `value_id` | varchar | CS2 device path |

### `device_states` — Real-Time Device Snapshot

Stores the complete current state of all ~50 connected devices as JSONB. **Fully replaced** on every sync cycle (no `id` column — see sync script).

**Device inventory:**

| Type | Count | Examples |
|------|-------|---------|
| Valve-basic | 24 | V104, V113, V203, V403, V503H, … |
| Pfeiffer gauges | 7 | RPT200 ×4, CPT200 ×2, MPT200 ×1 |
| Pumps | 4 | Turbopump, Kashiyama NeoDry, Agilent IDP7/IDP3 |
| Cryomech compressor | 1 | Pulse tube compressor |
| Temperature controllers | 2 | BlueFors TC, LakeShore controller |
| Flow controller | 1 | Bronkhorst EL-Flow |
| System modules | 5 | PLCAlarms, PLCRemote, GHSdiagnostics, CSState, CoreUnitLed |

### `alerts` — CS2 Alert Log

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint | Primary key |
| `code` | integer | Alert code |
| `datetime` | timestamptz | Time raised |
| `title` / `description` | varchar | Alert content |
| `severity` | integer | 1 = warning, 2 = error |
| `resolution_datetime` | timestamptz | Resolved time (null if open) |
| `resolved_by` | varchar | Operator who resolved |

This table is the source for the `check_cs2_alerts()` function in `monitor.py`, which forwards `severity ≥ 2` alerts to Slack.

### `automation_events` — Procedure Execution Log

Records every automated cool-down, warm-up, and condensation procedure run by CS2, including step-by-step state and elapsed time. Useful for correlating sensor anomalies with specific automation phases.

### `user_log_entries` — Operator Log

Manually written notes by lab personnel during operations, stored with author and timestamp. Max 2048 characters per entry.

---

## Appendix C — Data Flow & Migration Notes

### Full Data Flow

```
[BlueFors Sensors & Actuators]
        │  sampled every second
        ▼
[CS2 Control PC — Windows, 172.31.255.10]
  PostgreSQL 14.9 on port 5434
        │
        │  sync_push.ps1 — Windows Scheduled Task, every 1 min
        │  pushes new rows via \copy CSV over TCP port 5432
        ▼
[Raspberry Pi — 172.31.255.62]
  PostgreSQL 17 on port 5432
  data stored on 12.7 TB external drive
        │
        │  monitor.py — cron job, every 1 min
        ▼
[Slack #bluefors-alerts channel]
  threshold alerts + CS2 error forwarding
  interactive command interface (@BlueFors-Alert)
```

### Why Windows Pushes to Pi (not the reverse)

The CS2 Windows PC has no open inbound TCP port — the Pi cannot initiate a connection to it. Instead, the Windows Task Scheduler runs `sync_push.ps1` every minute, which opens an outbound connection to the Pi on port 5432 and pushes new rows via `psql \copy`.

### Initial Database Migration

The CS2 database (~2.1 GB, 12 M+ rows across 13 tables) was exported from the Windows CS2 PC using `pg_dump` and restored on the Pi:

```bash
# Export (Windows)
pg_dump -h localhost -p 5434 -U postgres -d cs2 -F c -f cs2_backup.dump

# Restore (Pi)
pg_restore -h localhost -U postgres -d cs2 -v cs2_backup.dump
```

The sync script initialises from the Pi's current `MAX(id)` per table on first run, so no historical rows are re-sent after the initial restore.

### Storage

The database is stored on a mounted external hard drive (`/mnt/harddrive/cs2_database/`) rather than the Pi's SD card, configured via `data_directory` in `postgresql.conf`. This avoids SD card wear and accommodates the growing dataset (1 sample/second × 50+ channels ≈ ~150 MB/day uncompressed).
