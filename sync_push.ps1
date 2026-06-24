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
$LocalPort  = 5434
$LocalUser  = "postgres"
$LocalPass  = "postgres"
$LocalDB    = "cs2"

$RemoteHost = "172.31.255.62"
$RemotePort = 5432
$RemoteUser = "postgres"
$RemotePass = "cs2monitor"
$RemoteDB   = "cs2"

$StateFile    = Join-Path $PSScriptRoot "win_sync_state.json"
$LogFile      = Join-Path $PSScriptRoot "win_sync.log"
$BatchSize    = 20000   # rows per table per cycle
$SyncInterval = 10      # seconds between sync cycles

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

function SyncOnce() {
    # Test remote connection
    $test = RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "SELECT 1"
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: Cannot connect to Raspberry Pi $RemoteHost"
        return
    }

    # Test local CS2 connection
    $env:PGPASSWORD = $LocalPass
    $localTest = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB -t -A `
        -c "SELECT COUNT(*) FROM double_value_change_events;" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Log "ERROR: Cannot connect to local CS2 database - $localTest"
        return
    }

    # Load or initialise state
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

    # device_states: full refresh (no id column)
    $env:PGPASSWORD = $LocalPass
    $dsData = & $psql -h $LocalHost -p $LocalPort -U $LocalUser -d $LocalDB `
        -c "\copy (SELECT * FROM public.device_states) TO stdout WITH CSV" 2>$null
    $dsLines = @($dsData | Where-Object { $_ -and $_ -ne "" })
    if ($dsLines.Count -gt 0) {
        $env:PGPASSWORD = $RemotePass
        RunPsql $RemoteHost $RemotePort $RemoteUser $RemotePass $RemoteDB "TRUNCATE public.device_states" | Out-Null
        $dsLines | & $psql -h $RemoteHost -p $RemotePort -U $RemoteUser -d $RemoteDB `
            -c "\copy public.device_states FROM stdin WITH CSV" 2>$null | Out-Null
    }

    SaveState $state
    if ($total -gt 0) {
        Log "=== Sync done: +$total rows ==="
    }
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
    # No execution time limit — script runs as a continuous loop
    $settings = New-ScheduledTaskSettingsSet `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) `
                    -MultipleInstances IgnoreNew
    Unregister-ScheduledTask -TaskName "BlueForsSync" -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName "BlueForsSync" `
        -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
    Write-Host "Scheduled task 'BlueForsSync' installed - syncs every $SyncInterval seconds." -ForegroundColor Green
    exit 0
}

# --- Main: continuous sync loop ---
Log "=== BlueFors sync started (every $SyncInterval seconds, batch $BatchSize rows) ==="
while ($true) {
    SyncOnce
    Start-Sleep -Seconds $SyncInterval
}
