# 以管理员身份运行此脚本
# 功能：开放PostgreSQL 5432端口，允许树莓派(172.31.255.62)连接

# 1. 添加防火墙规则
Write-Host "添加防火墙规则..." -ForegroundColor Cyan
netsh advfirewall firewall add rule `
    name="PostgreSQL-BlueFors-Monitor" `
    protocol=TCP `
    dir=in `
    localport=5432 `
    action=allow `
    remoteip=172.31.255.0/24

# 2. 找到PostgreSQL数据目录
$pgVersions = @("14", "15", "16", "17")
$pgDataDir = $null
foreach ($v in $pgVersions) {
    $candidate = "C:\Program Files\PostgreSQL\$v\data"
    if (Test-Path $candidate) {
        $pgDataDir = $candidate
        Write-Host "找到PostgreSQL数据目录: $pgDataDir" -ForegroundColor Green
        break
    }
}
if (-not $pgDataDir) {
    Write-Host "未找到PostgreSQL数据目录，请手动指定" -ForegroundColor Red
    exit 1
}

# 3. 修改 postgresql.conf：让PostgreSQL监听所有接口
$pgConf = Join-Path $pgDataDir "postgresql.conf"
$content = Get-Content $pgConf -Raw
if ($content -match "#?listen_addresses\s*=\s*'[^']*'") {
    $content = $content -replace "#?listen_addresses\s*=\s*'[^']*'", "listen_addresses = '*'"
    Set-Content $pgConf $content -Encoding UTF8
    Write-Host "postgresql.conf: listen_addresses 已设为 '*'" -ForegroundColor Green
} else {
    Add-Content $pgConf "`nlisten_addresses = '*'"
    Write-Host "postgresql.conf: 已追加 listen_addresses = '*'" -ForegroundColor Green
}

# 4. 修改 pg_hba.conf：允许树莓派网段连接
$pgHba = Join-Path $pgDataDir "pg_hba.conf"
$hbaLine = "host    cs2     postgres        172.31.255.0/24         md5"
$hbaContent = Get-Content $pgHba -Raw
if ($hbaContent -notmatch "172\.31\.255\.0") {
    Add-Content $pgHba "`n$hbaLine"
    Write-Host "pg_hba.conf: 已添加树莓派访问规则" -ForegroundColor Green
} else {
    Write-Host "pg_hba.conf: 规则已存在，跳过" -ForegroundColor Yellow
}

# 5. 重启PostgreSQL服务
Write-Host "重启PostgreSQL服务..." -ForegroundColor Cyan
$svc = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue
if ($svc) {
    Restart-Service -Name $svc.Name -Force
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $svc.Name
    Write-Host "PostgreSQL服务状态: $($svc.Status)" -ForegroundColor Green
} else {
    Write-Host "未找到PostgreSQL服务，请手动重启" -ForegroundColor Red
}

Write-Host "`n完成！树莓派现在可以连接到此电脑的PostgreSQL了。" -ForegroundColor Green
