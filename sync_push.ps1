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
$LocalPort  = 5432
$LocalUser  = "postgres"
$LocalPass  = "postgres"
$LocalDB    = "cs2"

$RemoteHost = "172.31.255.62"
$RemotePort = 5432
$RemoteUser = "postgres"
$RemotePass = "SoupR"
$RemoteDB   = "cs2"

$StateFile = Join-Path $PSScriptRoot "win_sync_state.json"
$LogFile   = Join-Path $PSScriptRoot "win_sync.log"
$BatchSize = 5000

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
        # Build hashtable manually (compatible with PowerShell 5.1)
        $ht = @{}
        foreach ($prop in $json.PSObject.Properties) {
            $ht[$prop.Name] = @{ last_id = [int]$prop.Value.last_id }
        }
        return $ht
    }
    return @{}
}

function SaveState($state) {
    $state | ConvertTo-Json -Depth 3 | Set-Content $StateFile
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
                    -RepetitionInterval (New-TimeSpan -Minutes 1) `
                    -RepetitionDuration ([TimeSpan]::MaxValue)
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

$state = LoadState
$total = 0

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

foreach ($tbl in $tables) {
    $lastId = 0
    if ($state.ContainsKey($tbl)) { $lastId = $state[$tbl].last_id }

    # Export new rows from local DB (stderr suppressed)
    $env:PGPASSWORD = $LocalPass
    $copyOut = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
        -c "\copy (SELECT * FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) TO stdout WITH CSV" `
        2>$null

    $lines = @($copyOut | Where-Object { $_ -and $_ -ne "" })
    if ($lines.Count -eq 0) { continue }

    # Import to Raspberry Pi (stderr suppressed)
    $env:PGPASSWORD = $RemotePass
    $lines | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
        -c "\copy public.$tbl FROM stdin WITH CSV ON CONFLICT (id) DO NOTHING" `
        2>$null | Out-Null

    if ($LASTEXITCODE -eq 0) {
        # Get the max id actually in this batch (subquery avoids upper-bound range bug with sparse IDs)
        $newId = RunPsql $LocalHost $LocalPort $LocalUser $LocalPass $LocalDB `
            "SELECT id FROM (SELECT id FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) t ORDER BY id DESC LIMIT 1"
        $newId = ($newId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if ($newId) {
            $state[$tbl] = @{ last_id = [int]$newId }
            $total += $lines.Count
            Log "  $tbl : +$($lines.Count) rows (last id: $newId)"
        }
    } else {
        Log "  $tbl : import failed"
    }
}

# device_states: full UPSERT (no id column, keyed by device_id)
$env:PGPASSWORD = $LocalPass
$dsData = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
    -c "\copy (SELECT * FROM public.device_states) TO stdout WITH CSV" 2>$null
$dsLines = @($dsData | Where-Object { $_ -and $_ -ne "" })
if ($dsLines.Count -gt 0) {
    $env:PGPASSWORD = $RemotePass
    $dsLines | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
        -c "\copy public.device_states FROM stdin WITH CSV ON CONFLICT (device_id) DO UPDATE SET datetime=EXCLUDED.datetime, values=EXCLUDED.values" `
        2>$null | Out-Null
    Log "  device_states: updated $($dsLines.Count) devices"
}

SaveState $state
Log "=== Sync done: +$total rows total ==="
