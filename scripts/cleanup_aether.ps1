# 精准清理 Aether 进程（避免多进程抢摄像头/抢端口，不误伤其它 python）。
#
# 用法：
#   cleanup_aether.ps1            只清后端（启动前用）
#   cleanup_aether.ps1 -All       连 ha_simulator 一起清（退出时用）
#
# 按命令行匹配，连 conda run 的父/子进程一起杀，
# 不受启动方式影响（手动 / 老脚本 / conda run 都能清干净）。
param(
    [switch]$All
)

$ErrorActionPreference = 'SilentlyContinue'

# 匹配规则：后端（uvicorn+8010）；-All 时再加 ha_simulator
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object {
        ($_.CommandLine -match 'uvicorn' -and $_.CommandLine -match '8010') -or
        ($All -and $_.CommandLine -match 'ha_simulator')
    }

if ($procs) {
    foreach ($p in $procs) {
        $cmd = ($p.CommandLine -replace '\s+', ' ')
        Write-Host "  killing PID $($p.ProcessId): $cmd"
        Stop-Process -Id $p.ProcessId -Force
        # 顺带杀掉以它为父进程的残留子进程
        Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.ProcessId)" |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    }
} else {
    Write-Host '  no existing Aether backend found'
}
