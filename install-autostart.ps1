# 注册「开机自启」计划任务：登录后静默后台运行 TimeMsg。
# 用法（普通用户权限即可）：  powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
# 卸载：                      powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1 -Remove

param(
    [switch]$Remove,
    [string]$TaskName = "TimeMsg"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已删除计划任务 $TaskName" -ForegroundColor Green
    } else {
        Write-Host "计划任务 $TaskName 不存在" -ForegroundColor Yellow
    }
    return
}

# 找到 pythonw.exe（绝对路径，避免计划任务里 PATH 不生效）
$pythonw = $null
$cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($cmd) { $pythonw = $cmd.Source }
if (-not $pythonw) {
    $candidates = @()
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter pythonw.exe -Recurse -ErrorAction SilentlyContinue
    $candidates += Get-ChildItem "$env:ProgramFiles\Python*" -Filter pythonw.exe -Recurse -ErrorAction SilentlyContinue
    if ($candidates.Count -gt 0) { $pythonw = $candidates[0].FullName }
}
if (-not $pythonw) {
    throw "找不到 pythonw.exe，请先安装 Python 3（winget install -e --id Python.Python.3.12）。"
}
Write-Host "使用解释器：$pythonw"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m app.main serve" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "已注册计划任务 $TaskName，下次登录 Windows 时自动后台运行。" -ForegroundColor Green
Write-Host "立即启动一次： Start-ScheduledTask -TaskName $TaskName"
Write-Host "提醒：请确认电脑不会在夜间睡眠，否则早上手机拉不到内容。见 README 的「电源设置」。"
