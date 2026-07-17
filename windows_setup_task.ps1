# 在Windows上以管理员身份运行
# 功能：安装Python依赖，并设置每分钟自动同步的计划任务

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonScript = Join-Path $ScriptDir "windows_sync_push.py"

Write-Host "=== BlueFors Monitor Windows Setup ===" -ForegroundColor Cyan

# 1. 检查Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "未找到Python，请先安装 Python 3.x (https://python.org)" -ForegroundColor Red
    exit 1
}
Write-Host "Python: $($python.Source)" -ForegroundColor Green

# 2. 安装psycopg2
Write-Host "安装psycopg2..." -ForegroundColor Cyan
python -m pip install psycopg2-binary --quiet
Write-Host "psycopg2 安装完成" -ForegroundColor Green

# 3. 测试连接
Write-Host "测试数据库连接..." -ForegroundColor Cyan
python $PythonScript
if ($LASTEXITCODE -ne 0) {
    Write-Host "连接测试失败，请检查网络和密码" -ForegroundColor Red
    exit 1
}
Write-Host "连接测试成功！" -ForegroundColor Green

# 4. 注册每分钟计划任务
$action  = New-ScheduledTaskAction -Execute "python" -Argument $PythonScript -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes 1) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName "BlueForsSync" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName "BlueForsSync" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force

Write-Host "计划任务 'BlueForsSync' 已注册，每分钟自动同步" -ForegroundColor Green
Write-Host "查看日志: $ScriptDir\win_sync.log" -ForegroundColor Yellow
