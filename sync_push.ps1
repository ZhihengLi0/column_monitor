# BlueFors CS2 -> Raspberry Pi data sync script
# Uses psql.exe (bundled with PostgreSQL) - no Python or extra installs needed.
# Compatible with Windows PowerShell 5.1+
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1           # run once
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1 -Install  # register scheduled task (every minute)

param([switch]$Install)

# --- Configuration ---
$LocalHost  = "localhost"
$LocalPort  = 5434
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

# --- Hardening (2026-11) ---
# When the Pi reboots mid-COPY, psql used to hang on the dead TCP connection
# for ~2 hours (Windows TCP keepalive default) and every scheduled run was
# skipped meanwhile. Connection-level timeouts + keepalives fix that; a
# statement timeout bounds a single stuck query; and a stale instance older
# than MaxRunMinutes is killed at start.
$env:PGCONNECT_TIMEOUT = "10"
$env:PGOPTIONS         = "-c statement_timeout=120000"
$RemoteConn = "host=$RemoteHost port=$RemotePort user=$RemoteUser dbname=$RemoteDB " +
              "connect_timeout=10 keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=3"
$LocalConn  = "host=$LocalHost port=$LocalPort user=$LocalUser dbname=$LocalDB connect_timeout=10"
$MaxRunMinutes = 5

# Kill any previous instance of this script that has been running too long.
$me = $PID
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" | Where-Object {
    $_.ProcessId -ne $me -and $_.CommandLine -like "*sync_push.ps1*" -and
    ((Get-Date) - $_.CreationDate).TotalMinutes -gt $MaxRunMinutes
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Add-Content $LogFile "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') killed stale sync instance PID $($_.ProcessId)"
}

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
    $conn = if ($h -eq $RemoteHost) { $RemoteConn } else { $LocalConn }
    $result = & $psql "$conn" -t -A -c $sql 2>$null
    return $result
}

function LoadState() {
    if (Test-Path $StateFile) {
        $json = Get-Content $StateFile -Raw | ConvertFrom-Json
        $ht = @{}
        foreach ($prop in $json.PSObject.Properties) {
            $ht[$prop.Name] = @{ last_id = [long]$prop.Value.last_id }
        }
        return $ht
    }
    return $null
}

function SaveState($state) {
    $state | ConvertTo-Json -Depth 3 | Set-Content $StateFile
}

function InitStateFromRemote() {
    # On first run: start from the Raspberry Pi's current max IDs
    # This skips re-syncing the backup data already on the Pi
    Log "First run - initialising sync position from Raspberry Pi..."
    $state = @{}
    foreach ($tbl in $tables) {
        $maxId = RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB `
            "SELECT COALESCE(MAX(id), 0) FROM public.$tbl"
        $maxId = ($maxId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if (-not $maxId) { $maxId = 0 }
        $state[$tbl] = @{ last_id = [long]$maxId }
        Log "  $tbl : will sync from id > $maxId"
    }
    return $state
}

# --- Install as scheduled task ---
if ($Install) {
    $script = $PSCommandPath
    # Delete old task if exists
    schtasks /delete /tn "BlueForsSync" /f 2>$null
    # Create task running as SYSTEM — works without interactive login
    $tr = "powershell.exe -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File $script"
    schtasks /create /tn "BlueForsSync" /tr $tr /sc minute /mo 1 /ru SYSTEM /f
    Write-Host "Scheduled task 'BlueForsSync' installed - runs every minute." -ForegroundColor Green
    exit 0
}

# --- Main sync ---
Log "=== Sync started ==="

# Test remote connection
$test = RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "SELECT 1"
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Cannot connect to Raspberry Pi $RemoteHost"
    exit 1
}
Log "Connected to Raspberry Pi OK"

# Test local CS2 connection
$env:PGPASSWORD = $LocalPass
$localTest = & $psql "$LocalConn" -t -A `
    -c "SELECT COUNT(*) FROM double_value_change_events;" 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Cannot connect to local CS2 database - $localTest"
    exit 1
}
Log "Local CS2 database OK - $localTest rows in double_value_change_events"

# Load or initialise state
# If state file missing OR empty (no tables tracked yet), init from Raspberry Pi
$state = LoadState
if ($null -eq $state -or $state.Count -eq 0) {
    $state = InitStateFromRemote
    SaveState $state
}

$total = 0

foreach ($tbl in $tables) {
    $lastId = 0
    if ($state.ContainsKey($tbl)) { $lastId = $state[$tbl].last_id }

    # Export new rows from local CS2
    $env:PGPASSWORD = $LocalPass
    $copyOut = & $psql "$LocalConn" `
        -c "\copy (SELECT * FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) TO stdout WITH CSV" `
        2>$null

    $lines = @($copyOut | Where-Object { $_ -and $_ -ne "" })
    if ($lines.Count -eq 0) { continue }

    # Import to Raspberry Pi (no ON CONFLICT needed - we only send new rows)
    $env:PGPASSWORD = $RemotePass
    $importErr = $lines | & $psql "$RemoteConn" `
        -c "\copy public.$tbl FROM stdin WITH CSV" 2>&1

    if ($LASTEXITCODE -eq 0) {
        $newId = RunPsql $LocalHost $LocalPort $LocalUser $LocalPass $LocalDB `
            "SELECT id FROM (SELECT id FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) t ORDER BY id DESC LIMIT 1"
        $newId = ($newId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if ($newId) {
            $state[$tbl] = @{ last_id = [long]$newId }
            $total += $lines.Count
            Log "  $tbl : +$($lines.Count) rows (last id: $newId)"
        }
    } else {
        Log "  $tbl : import failed - $importErr"
    }
}

# device_states: full refresh (no id column)
$env:PGPASSWORD = $LocalPass
$dsData = & $psql "$LocalConn" `
    -c "\copy (SELECT * FROM public.device_states) TO stdout WITH CSV" 2>$null
$dsLines = @($dsData | Where-Object { $_ -and $_ -ne "" })
if ($dsLines.Count -gt 0) {
    $env:PGPASSWORD = $RemotePass
    # Clear and re-import device states
    RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "TRUNCATE public.device_states" | Out-Null
    $dsLines | & $psql "$RemoteConn" `
        -c "\copy public.device_states FROM stdin WITH CSV" 2>$null | Out-Null
    Log "  device_states: updated $($dsLines.Count) devices"
}

SaveState $state
Log "=== Sync done: +$total rows total ==="
