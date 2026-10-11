# 清理 Clash for Windows 残留: 持久路由 + cfw-tap 虚拟网卡
# 需要以管理员身份运行(脚本会自己检查并提示)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('cleanup_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.log')

$isAdmin = (New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "请以管理员身份运行本脚本(右键 - 以管理员身份运行 PowerShell 后再执行)。" -ForegroundColor Red
    exit 1
}

Start-Transcript -Path $log -Force | Out-Null
Write-Host "日志: $log"

Write-Host "`n== 1. 删除残留持久路由 0.0.0.0/0 -> 10.0.0.0 ==" -ForegroundColor Cyan
route delete 0.0.0.0 mask 0.0.0.0 10.0.0.0
Write-Host "剩余持久路由:"
route print -4 | Select-String -Pattern "Persistent" -Context 0,4

Write-Host "`n== 2. 移除残留虚拟网卡 cfw-tap (ROOT\NET\0001) ==" -ForegroundColor Cyan
pnputil /remove-device "ROOT\NET\0001"

Write-Host "`n== 3. 当前网络适配器 ==" -ForegroundColor Cyan
Get-NetAdapter -IncludeHidden |
    Where-Object { $_.InterfaceDescription -match 'TAP|Wintun|WireGuard' } |
    Select-Object Name, InterfaceDescription, ifIndex, Status | Format-Table -AutoSize

Stop-Transcript | Out-Null
Write-Host "完成。日志: $log" -ForegroundColor Green
