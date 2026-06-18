# BlueFors CS2 -> Raspberry Pi data sync script
# Uses psql.exe (bundled with PostgreSQL) — no Python or extra installs needed.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1           # run once
#   powershell -ExecutionPolicy Bypass -File sync_push.ps1 -Install  # register as scheduled task (every minute)

param([switch]$Install)

# ── Configuration ─────────────────────────────────────────────────────────
$LocalPG  = @{ host="localhost";      port=5432; user="postgres"; pass="postgres";    db="cs2" }
$RemotePG = @{ host="172.31.255.62";  port=5432; user="postgres"; pass="cs2monitor"; db="cs2" }
$StateFile = Join-Path $PSScriptRoot "win_sync_state.json"
$LogFile   = Join-Path $PSScriptRoot "win_sync.log"
$BatchSize = 5000
# ──────────────────────────────────────────────────────────────────────────

# Locate psql.exe
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

function RunPsql($cfg, $sql) {
    $env:PGPASSWORD = $cfg.pass
    return & $psql -h $cfg.host -p $cfg.port -U $cfg.user -d $cfg.db -t -A -c $sql 2>&1
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

# ── Install as scheduled task ──────────────────────────────────────────────
if ($Install) {
    $script   = $MyInvocation.MyCommand.Path
    $action   = New-ScheduledTaskAction -Execute "powershell.exe" `
                    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
                    -WorkingDirectory $PSScriptRoot
    $trigger  = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 1) -Once -At (Get-Date)
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Unregister-ScheduledTask -TaskName "BlueForsSync" -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName "BlueForsSync" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
    Write-Host "Scheduled task 'BlueForsSync' installed — runs every minute." -ForegroundColor Green
    exit 0
}

# ── Main sync ─────────────────────────────────────────────────────────────
Log "=== Sync started ==="

$test = RunPsql $RemotePG "SELECT 1"
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: Cannot connect to Raspberry Pi $($RemotePG.host): $test"
    exit 1
}

$state = LoadState
$total = 0

$tables = @(
    @{ name="double_value_change_events";  id="id" },
    @{ name="int_value_change_events";     id="id" },
    @{ name="boolean_value_change_events"; id="id" },
    @{ name="string_value_change_events";  id="id" },
    @{ name="json_value_change_events";    id="id" },
    @{ name="device_events";               id="id" },
    @{ name="alerts";                      id="id" },
    @{ name="automation_events";           id="id" },
    @{ name="user_log_entries";            id="id" }
)

foreach ($t in $tables) {
    $tbl    = $t.name
    $idCol  = $t.id
    $lastId = if ($state.ContainsKey($tbl)) { $state[$tbl].last_id } else { 0 }

    $env:PGPASSWORD = $LocalPG.pass
    $copyOut = & $psql -h $LocalPG.host -p $LocalPG.port -U $LocalPG.user -d $LocalPG.db `
        -c "\copy (SELECT * FROM public.$tbl WHERE $idCol > $lastId ORDER BY $idCol LIMIT $BatchSize) TO stdout WITH CSV" 2>&1

    $lines = @($copyOut | Where-Object { $_ -ne "" })
    if ($lines.Count -eq 0) { continue }

    $env:PGPASSWORD = $RemotePG.pass
    $lines | & $psql -h $RemotePG.host -p $RemotePG.port -U $RemotePG.user -d $RemotePG.db `
        -c "\copy public.$tbl FROM stdin WITH CSV ON CONFLICT ($idCol) DO NOTHING" 2>&1 | Out-Null

    if ($LASTEXITCODE -eq 0) {
        $env:PGPASSWORD = $LocalPG.pass
        $newId = RunPsql $LocalPG "SELECT MAX($idCol) FROM public.$tbl WHERE $idCol > $lastId AND $idCol <= $($lastId + $BatchSize)"
        $newId = ($newId | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
        if ($newId) {
            $state[$tbl] = @{ last_id = [int]$newId }
            $total += $lines.Count
            Log "  $tbl : +$($lines.Count) rows"
        }
    }
}

# device_states: full UPSERT (no id, keyed by device_id)
$env:PGPASSWORD = $LocalPG.pass
$dsData = & $psql -h $LocalPG.host -p $LocalPG.port -U $LocalPG.user -d $LocalPG.db `
    -c "\copy (SELECT * FROM public.device_states) TO stdout WITH CSV" 2>&1
if ($dsData) {
    $env:PGPASSWORD = $RemotePG.pass
    $dsData | & $psql -h $RemotePG.host -p $RemotePG.port -U $RemotePG.user -d $RemotePG.db `
        -c "\copy public.device_states FROM stdin WITH CSV ON CONFLICT (device_id) DO UPDATE SET datetime=EXCLUDED.datetime, values=EXCLUDED.values" 2>&1 | Out-Null
}

SaveState $state
Log "=== Sync done: +$total rows total ==="
