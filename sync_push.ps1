# BlueFors CS2 -> Raspberry Pi data sync script
# Uses psql.exe (bundled with PostgreSQL) - no Python or extra installs needed.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1           # run once
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1 -Install  # register scheduled task

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
$RemotePass = "cs2monitor"
$RemoteDB   = "cs2"

$StateFile  = Join-Path $PSScriptRoot "win_sync_state.json"
$LogFile    = Join-Path $PSScriptRoot "win_sync.log"
$BatchSize  = 5000

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
    return & $psql -h $h -p $p -U $u -d $db -t -A -c $sql 2>&1
}

function LoadState() {
    if (Test-Path $StateFile) {
        return Get-Content $StateFile | ConvertFrom-Json -AsHashtable
    }
    return @{}
}

function SaveState($state) {
    $state | ConvertTo-Json | Set-Content $StateFile
}

# --- Install as scheduled task ---
if ($Install) {
    $script   = $MyInvocation.MyCommand.Path
    $action   = New-ScheduledTaskAction `
                    -Execute "powershell.exe" `
                    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
                    -WorkingDirectory $PSScriptRoot
    $trigger  = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 1) -Once -At (Get-Date)
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Unregister-ScheduledTask -TaskName "BlueForsSync" -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName "BlueForsSync" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
    Write-Host "Scheduled task 'BlueForsSync' installed - runs every minute." -ForegroundColor Green
    exit 0
}

# --- Main sync ---
Log "=== Sync started ==="

$test = RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "SELECT 1"
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Cannot connect to Raspberry Pi ${RemoteHost}: $test"
    exit 1
}

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

    $env:PGPASSWORD = $LocalPass
    $copyOut = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
        -c "\copy (SELECT * FROM public.$tbl WHERE id > $lastId ORDER BY id LIMIT $BatchSize) TO stdout WITH CSV" 2>&1

    $lines = @($copyOut | Where-Object { $_ -ne "" })
    if ($lines.Count -eq 0) { continue }

    $env:PGPASSWORD = $RemotePass
    $lines | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
        -c "\copy public.$tbl FROM stdin WITH CSV ON CONFLICT (id) DO NOTHING" 2>&1 | Out-Null

    if ($LASTEXITCODE -eq 0) {
        $env:PGPASSWORD = $LocalPass
        $newId = RunPsql $LocalHost $LocalPort $LocalUser $LocalPass $LocalDB `
            "SELECT MAX(id) FROM public.$tbl WHERE id > $lastId AND id <= $($lastId + $BatchSize)"
        $newId = ($newId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if ($newId) {
            $state[$tbl] = @{ last_id = [int]$newId }
            $total += $lines.Count
            Log "  $tbl : +$($lines.Count) rows"
        }
    }
}

# device_states: full UPSERT (keyed by device_id, no id column)
$env:PGPASSWORD = $LocalPass
$dsData = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
    -c "\copy (SELECT * FROM public.device_states) TO stdout WITH CSV" 2>&1
if ($dsData) {
    $env:PGPASSWORD = $RemotePass
    $dsData | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
        -c "\copy public.device_states FROM stdin WITH CSV ON CONFLICT (device_id) DO UPDATE SET datetime=EXCLUDED.datetime, values=EXCLUDED.values" 2>&1 | Out-Null
}

SaveState $state
Log "=== Sync done: +$total rows total ==="
