# install_sync_task.ps1 —— 注册 Windows 计划任务，自动同步 HA 配置 IP
#
# 做两件事：
#   1. 开机时运行一次 sync_ha_ip.sh
#   2. 之后每 10 分钟运行一次（捕获 DHCP 续约导致的 IP 变化）
#
# 用法（以管理员身份在 PowerShell 中运行）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_sync_task.ps1
#
# 卸载：schtasks /Delete /TN "Aether\SyncHaIp" /F

param(
    [string]$BashExe = "C:\Program Files\Git\git-bash.exe",
    [string]$ScriptRel = "scripts/sync_ha_ip.sh"
)

$ErrorActionPreference = "Stop"

# 定位仓库根目录（脚本在 scripts/ 下，往上一层即仓库根）
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot $ScriptRel

if (-not (Test-Path $ScriptPath)) {
    Write-Host "ERROR: 找不到 $ScriptPath" -ForegroundColor Red
    exit 1
}

# Git Bash 存在性检查（git-bash.exe 会弹出窗口，用 bash.exe 更安静）
$QuietBash = "C:\Program Files\Git\bin\bash.exe"
if (-not (Test-Path $QuietBash)) {
    Write-Host "WARN: 未找到 $QuietBash，将使用 git-bash.exe（会弹窗）" -ForegroundColor Yellow
    $BashToUse = $BashExe
} else {
    $BashToUse = $QuietBash
}

# 任务命令：先做日志截断（超 1MB 保留末尾 200 行），再跑同步脚本追加日志。
# 截断必须在 ">> $log" 重定向打开之前完成——否则 truncate/mv 换掉 inode，
# 已打开的追加句柄写进旧文件，日志从此静默停更。
# 每 10 分钟一行，200 行 ≈ 1.4 天；此前纯 >> 追加无轮转，一年约 10MB 慢性增长。
$LogCmd = 'log=logs/sync_ha_ip.log; [ -f $log ] && [ $(stat -c%s $log) -gt 1048576 ] && { tail -n 200 $log > $log.t && mv $log.t $log; }; bash scripts/sync_ha_ip.sh >> $log 2>&1'
$Action = "$BashToUse -lc 'cd /d/Aether && $LogCmd'"
$TaskName = "Aether\SyncHaIp"

# 删除旧任务（若存在）
schtasks /Query /TN $TaskName 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    schtasks /Delete /TN $TaskName /F | Out-Null
    Write-Host "已删除旧任务"
}

# 创建任务：开机时运行 + 每 10 分钟重复，最高权限
schtasks /Create /TN $TaskName /TR $Action /SC MINUTE /MO 10 /RL HIGHEST /F | Out-Null

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: 计划任务创建失败" -ForegroundColor Red
    exit 1
}

# 立即运行一次
schtasks /Run /TN $TaskName | Out-Null

Write-Host ""
Write-Host "✓ 计划任务已创建: $TaskName" -ForegroundColor Green
Write-Host "  - 触发: 每 10 分钟自动检测 IP 变化"
Write-Host "  - 命令: bash scripts/sync_ha_ip.sh"
Write-Host "  - 日志: D:\Aether\logs\sync_ha_ip.log"
Write-Host ""
Write-Host "管理任务:" -ForegroundColor Cyan
Write-Host "  查看:   schtasks /Query /TN Aether\SyncHaIp /V /FO LIST"
Write-Host "  立即运行: schtasks /Run /TN Aether\SyncHaIp"
Write-Host "  卸载:   schtasks /Delete /TN Aether\SyncHaIp /F"
