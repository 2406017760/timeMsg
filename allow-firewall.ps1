# 允许手机通过局域网访问 TimeMsg 的 HTTP 端口（否则手机会连不上）。
# 必须用【管理员】PowerShell 运行：
#   powershell -ExecutionPolicy Bypass -File .\allow-firewall.ps1
# 删除规则：
#   Remove-NetFirewallRule -DisplayName "TimeMsg (Python)"

param(
    [int]$Port = 8765,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "请用管理员身份的 PowerShell 运行本脚本。" -ForegroundColor Red
    exit 1
}

$name = "TimeMsg (Python)"

if ($Remove) {
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Write-Host "已删除防火墙规则 $name" -ForegroundColor Green
    return
}

Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
Write-Host "已允许「专用网络」入站 TCP $Port（规则名：$name）" -ForegroundColor Green
Write-Host "如果手机还是连不上：确认电脑当前网络在 Windows 里被标记为「专用网络」，而不是「公用网络」。"
