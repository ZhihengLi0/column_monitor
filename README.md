# BlueFors CS2 Monitor

Real-time monitoring and Slack alerting for a BlueFors dilution refrigerator (CS2 control system).  
Data is synced every minute from the Windows CS2 PC to a Raspberry Pi, which runs anomaly detection and sends Slack notifications.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Network](#network)
3. [Raspberry Pi Setup](#raspberry-pi-setup)
4. [Database Backup Restore](#database-backup-restore)
5. [Windows Sync Script — sync_push.ps1](#windows-sync-script--sync_pushps1)
6. [Monitor Script — monitor.py](#monitor-script--monitorpy)
7. [Configuration — config.py](#configuration--configpy)
8. [Slack App Setup](#slack-app-setup)
9. [Starting the System](#starting-the-system)
10. [Slack Interaction](#slack-interaction)
11. [Database Schema](#database-schema)
12. [Sensor Mappings](#sensor-mappings)
13. [Troubleshooting](#troubleshooting)
14. [File Reference](#file-reference)

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
pip3 install psycopg2-binary requests
```

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
| 1 | `check_acknowledgements()` | Polls Slack for reactions (✅ 👏 👍 🤙) or thread replies (`ok`/`OK`) on previous alert messages; silences that sensor for 10 min if found |
| 2 | `check_commands()` | Reads new `@BlueFors-Alert` mentions in the channel; parses and executes threshold-change or ack commands |
| 3 | `check_data_freshness()` | Queries `MAX(time)` from `double_value_change_events`; fires an alert if data is more than 5 minutes old (with 30-min cooldown to avoid spam) |
| 4 | `check_sensor_thresholds()` | For each sensor in `THRESHOLDS`: fetches latest value, compares to limit, sends alert if exceeded and cooldown has passed |
| 5 | `check_cs2_alerts()` | Fetches new rows from the `alerts` table with `severity >= CS2_ALERT_MIN_SEVERITY`; batches by error code and forwards to Slack |
| 6 | Send + track | Sends all queued Slack messages; saves each message's `ts` (timestamp) so reactions on it can be checked next run |
| 7 | `save_state()` | Writes `monitor_state.json` — persists last alert times, last CS2 alert ID, acknowledgements, threshold overrides |

### State file (monitor_state.json)

```json
{
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
  "threshold_overrides": {
    "MXC_TEMPERATURE": { "max_val": 0.05, "min_val": null, "expires_at": "2026-06-18T14:33:00" }
  },
  "last_slack_ts": "1781830413.557959",
  "last_freshness_alert": "2026-06-18T14:00:00"
}
```

---

## Configuration — config.py

Located at `/home/cdms/bluefors_monitor/config.py` on the Raspberry Pi.  
**Not committed to GitHub** (contains credentials).

```python
# BlueFors Monitor Configuration

# Local PostgreSQL (Raspberry Pi)
LOCAL_PG_HOST     = "localhost"
LOCAL_PG_PORT     = 5432
LOCAL_PG_USER     = "postgres"
LOCAL_PG_PASSWORD = "cs2monitor"
LOCAL_PG_DB       = "cs2"

# Slack
SLACK_BOT_TOKEN   = "xoxb-..."          # from api.slack.com/apps → OAuth & Permissions
SLACK_CHANNEL     = "C0B42G4AU0N"       # channel ID (not name)
SLACK_BOT_USER_ID = "U0BBGRB0HC4"       # bot user ID — used to detect @mentions

# Sync batch size (rows per table per cycle)
SYNC_BATCH_SIZE = 5000

# Alert thresholds: (max_value, min_value, description)
# Set max_value=None for a lower-bound check, min_value=None for an upper-bound check
THRESHOLDS = {
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

# How long before the same sensor can alert again (minutes)
ALERT_COOLDOWN_MINUTES = 30

# Minimum CS2 alert severity to forward: 1 = warning, 2 = error
CS2_ALERT_MIN_SEVERITY = 2
```

To change a threshold permanently, edit this file and restart the cron job.  
To change it temporarily via Slack, use the `change` command (see [Slack Interaction](#slack-interaction)).

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

### Acknowledge an alert (silence 10 minutes)

React to any alert message with **✅ 👏 👍 🤙**, or reply `ok` / `OK` in the alert thread.  
The monitor checks for these each minute and silences that sensor for 10 minutes.

### Commands

Mention `@BlueFors-Alert` in the channel followed by a command:

| Command | Description |
|---|---|
| `help` | Show all available commands |
| `list` | Show all sensors with numbers, short names, and current thresholds |
| `status` | Show active threshold overrides and silenced sensors |
| `ack` | Silence ALL sensors for 10 minutes |
| `change <sensor> to <value> for 5min` | Temporary 5-minute threshold override |
| `change <sensor> to <value> for 10min` | Temporary 10-minute threshold override |
| `change <sensor> to <value> for ever` | Permanent threshold change (until `reset`) |
| `reset <sensor>` | Restore factory default threshold |

The `<sensor>` field accepts a number, short name, or full mapping name interchangeably.

### Sensor identifiers

| # | Short name | Full mapping name | Unit | Default alert condition |
|---|---|---|---|---|
| 1 | MXC | MXC_TEMPERATURE | K | > 0.030 K |
| 2 | MXCFAR | MXC_TEMPERATURE_FAR | K | > 0.050 K |
| 3 | STILL | STILL_TEMPERATURE | K | > 2.0 K |
| 4 | 4K | 4K_TEMPERATURE | K | > 6.0 K |
| 5 | 50K | 50K_TEMPERATURE | K | > 65.0 K |
| 6 | B1A | B1A_TEMPERATURE | K | > 1.0 K |
| 7 | B2 | B2_TEMPERATURE | K | > 4.5 K |
| 8 | P1 | P1_PRESSURE | mbar | > 20.0 mbar |
| 9 | P2 | P2_PRESSURE | mbar | > 0.5 mbar |
| 10 | P5 | P5_PRESSURE | mbar | > 1e-3 mbar |
| 11 | FLOW | FLOW_VALUE | mmol/s | < 0.01 mmol/s |

### Examples

```
@BlueFors-Alert list
@BlueFors-Alert status
@BlueFors-Alert ack
@BlueFors-Alert change 1 to 0.05 for 5min
@BlueFors-Alert change MXC to 0.04 for 10min
@BlueFors-Alert change MXC_TEMPERATURE to 0.035 for ever
@BlueFors-Alert reset STILL
@BlueFors-Alert reset 3
```

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

---

## File Reference

| File | Location | Description |
|---|---|---|
| `sync_push.ps1` | Windows `C:\bluefors_monitor\` | Pushes CS2 data to Pi every minute |
| `win_sync_state.json` | Windows `C:\bluefors_monitor\` | Tracks last synced row ID per table (auto-generated) |
| `win_sync.log` | Windows `C:\bluefors_monitor\` | Sync run log (auto-generated) |
| `config.py` | Pi `/home/cdms/bluefors_monitor/` | All credentials and threshold settings |
| `monitor.py` | Pi `/home/cdms/bluefors_monitor/` | Alert monitor — runs via cron every minute |
| `setup_cron.sh` | Pi `/home/cdms/bluefors_monitor/` | Installs the cron job |
| `monitor_state.json` | Pi `/home/cdms/bluefors_monitor/` | Runtime state — alert times, acks, overrides (auto-generated) |
| `monitor.log` | Pi `/home/cdms/bluefors_monitor/` | Monitor run log (auto-generated) |

---

## GitHub

Source: [https://github.com/ZhihengLi0/column](https://github.com/ZhihengLi0/column)

> `config.py` is excluded from the repository (contains the Slack token and database password).  
> After cloning, create it manually on the Pi using the template in the [Configuration](#configuration--configpy) section above.

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
