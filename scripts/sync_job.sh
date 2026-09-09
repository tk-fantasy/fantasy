#!/usr/bin/env bash
# sync_job.sh —— 计划任务的无窗口入口：先轮转日志，再跑同步脚本。
# 由 scripts/run_sync_hidden.vbs 经 wscript 调用（install_sync_task.ps1 注册）。
# 轮转必须发生在 ">> $log" 打开追加句柄之前——否则 mv 换掉 inode，
# 已打开的句柄写进旧文件，日志从此静默停更。
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
log=logs/sync_ha_ip.log
if [ -f "$log" ] && [ "$(stat -c%s "$log")" -gt 1048576 ]; then
  tail -n 200 "$log" > "$log.t" && mv "$log.t" "$log"
fi
bash scripts/sync_ha_ip.sh >> "$log" 2>&1
