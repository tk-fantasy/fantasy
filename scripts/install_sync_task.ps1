# install_sync_task.ps1 —— 注册 Windows 计划任务，自动同步 HA 配置 IP
#
# 行为：
#   1. 登录时运行一次（LogonTrigger）
#   2. 之后每 10 分钟静默检测一次（捕获 DHCP 续约导致的 IP 变化；
#      IP 没变脚本 1 秒内直接退出，变了才更新配置并重启 HA 容器）
#   3. 全程无窗口：任务动作是 wscript.exe + run_sync_hidden.vbs（窗口样式 0）。
#      注意：直接调 bash.exe 必然闪出控制台——任务计划程序没有控制台，
#      Windows 会为控制台程序新建可见窗口，"bash.exe 比 git-bash.exe 安静"是误解。
#   4. 单次执行超 5 分钟自动终止（防 bash 偶发卡死留僵尸进程/窗口挂着不消失）
#   5. 重叠触发忽略（IgnoreNew）
#
# 用法（须以管理员身份运行——本机策略拒绝非管理员注册计划任务，实测根目录
#      建任务也"拒绝访问"；但任务本身以最低权限注册，运行时不需要管理员，
#      卡死实例普通权限即可杀掉）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_sync_task.ps1
#
# 卸载：schtasks /Delete /TN "Aether\SyncHaIp" /F

param(
    # 脚本本身不需要管理员（netsh 查询/sed/docker/curl 均普通权限可用）。
    # 如确需最高权限，以管理员身份运行并传 -RunLevel Highest。
    [ValidateSet("Limited", "Highest")]
    [string]$RunLevel = "Limited"
)

$ErrorActionPreference = "Stop"

# EAP=Stop 下，原生命令写 stderr（如 /Query 任务不存在时）会炸脚本；
# 不能用 cmd /c "..." 包一层——内层再带引号时 cmd 不剥外层引号，参数会碎。
# 统一走这个函数：临时降级 EAP、吞掉输出、只回传退出码。
function Invoke-SchtasksQuiet {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    schtasks @args 2>&1 | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return $code
}

# 定位仓库根目录（脚本在 scripts/ 下，往上一层即仓库根）
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VbsPath  = Join-Path $PSScriptRoot "run_sync_hidden.vbs"
$JobPath  = Join-Path $PSScriptRoot "sync_job.sh"

foreach ($f in @($JobPath, $VbsPath)) {
    if (-not (Test-Path $f)) {
        Write-Host "ERROR: 找不到 $f" -ForegroundColor Red
        exit 1
    }
}

# run_sync_hidden.vbs 里硬编码了这个路径；Git 装在不同位置时改 vbs 保持一致
if (-not (Test-Path "C:\Program Files\Git\bin\bash.exe")) {
    Write-Host "WARN: 未找到 C:\Program Files\Git\bin\bash.exe，请确认 Git for Windows 安装路径" -ForegroundColor Yellow
}

$TaskName = "Aether\SyncHaIp"

# 删除旧任务（若存在）。旧版任务以最高权限注册时普通权限删不掉，必须显式报错，
# 否则 /Create 因同名残留失败，报错却指向 XML，误导排查。
if ((Invoke-SchtasksQuiet /Query /TN $TaskName) -eq 0) {
    if ((Invoke-SchtasksQuiet /Delete /TN $TaskName /F) -ne 0) {
        Write-Host "ERROR: 删除旧任务失败（多半是它以管理员权限注册的）。以管理员身份重跑本脚本即可。" -ForegroundColor Red
        exit 1
    }
    Write-Host "已删除旧任务"
}

# schtasks 命令行表达不了"登录触发 + 每 10 分钟重复 + 执行超时"，只能走任务 XML
# schtasks 的 XML 校验不接受显式 <RunLevel>Limited</RunLevel>——省略即默认 Limited，
# 只有 Highest 才输出该元素。UserId 用 SID，避免域/本地化差异。
$Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$RunLevelLine = if ($RunLevel -eq "Highest") { "      <RunLevel>HighestAvailable</RunLevel>`r`n" } else { "" }
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Aether: HA 配置 IP 自动同步（登录时 + 每 10 分钟静默检测，无窗口）</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <TimeTrigger>
      <StartBoundary>2026-08-05T00:00:00</StartBoundary>
      <Repetition>
        <Interval>PT10M</Interval>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$Sid</UserId>
      <LogonType>InteractiveToken</LogonType>
$RunLevelLine    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//B //nologo "$VbsPath"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$xmlFile = Join-Path $env:TEMP "aether_synchaip_task.xml"
[System.IO.File]::WriteAllText($xmlFile, $xml, [System.Text.Encoding]::Unicode)
$rc = Invoke-SchtasksQuiet /Create /TN $TaskName /XML $xmlFile /F

if ($rc -ne 0) {
    Write-Host "ERROR: 计划任务创建失败 (exit $rc)，生成的 XML 保留在 $xmlFile 供排查" -ForegroundColor Red
    exit 1
}
Remove-Item $xmlFile -ErrorAction SilentlyContinue

# 立即运行一次
Invoke-SchtasksQuiet /Run /TN $TaskName | Out-Null

Write-Host ""
Write-Host "√ 计划任务已创建: $TaskName（无窗口）" -ForegroundColor Green
Write-Host "  - 触发: 登录时一次 + 每 10 分钟静默检测 IP 变化"
Write-Host "  - 动作: wscript //B run_sync_hidden.vbs -> bash -l scripts/sync_job.sh"
Write-Host "  - 保险: 单次执行超 5 分钟自动终止；重叠触发忽略"
Write-Host "  - 日志: $RepoRoot\logs\sync_ha_ip.log"
Write-Host ""
Write-Host "管理任务:" -ForegroundColor Cyan
Write-Host "  查看:   schtasks /Query /TN Aether\SyncHaIp /V /FO LIST"
Write-Host "  立即运行: schtasks /Run /TN Aether\SyncHaIp"
Write-Host "  卸载:   schtasks /Delete /TN Aether\SyncHaIp /F"
