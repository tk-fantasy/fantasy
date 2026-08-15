#!/usr/bin/env bash
# Aether 恢复脚本（09 清单条目 6）—— 树莓派上运行
#
# 用法：./scripts/restore.sh backups/aether-backstamp-xxx.tar.gz
# 会覆盖当前 config.json / .env / HA 配置 / 数据库卷，执行前需确认。
set -euo pipefail

[ $# -eq 1 ] || { echo "用法: ./scripts/restore.sh <aether-backup-*.tar.gz>"; exit 1; }
BACKUP="$1"
[ -f "$BACKUP" ] || { echo "[restore][错误] 备份文件不存在: $BACKUP"; exit 1; }

log() { echo "[restore] $*"; }

echo "将用 $BACKUP 覆盖：config.json、.env、ha_config/、数据库卷（用户/会话/个性化全部回到备份时点）"
read -r -p "确认继续？(yes/no) " ans
[ "$ans" = "yes" ] || { log "已取消"; exit 0; }

# 先停应用，避免 SQLite 写入与恢复竞争
log "停止 aether 容器"
docker compose stop aether 2>/dev/null || true

log "恢复宿主侧文件（config/.env/HA/MQTT 配置）"
tar xzf "$BACKUP" config.json .env 2>/dev/null || tar xzf "$BACKUP" config.json || true
tar xzf "$BACKUP" ha_config mosquitto 2>/dev/null || log "包内无 ha_config/mosquitto（跳过）"

DATA_VOLUME=$(docker volume ls --format '{{.Name}}' | grep -E 'aether-data$' | head -1 || true)
if [ -n "$DATA_VOLUME" ] && tar tzf "$BACKUP" | grep -q '^data/'; then
  log "恢复数据卷 $DATA_VOLUME"
  docker run --rm -v "$DATA_VOLUME":/data -v "$(pwd)/$BACKUP":/backup.tar.gz alpine \
    sh -c "cd /data && tar xzf /backup.tar.gz data --strip-components=1"
else
  log "包内无 data/ 目录，跳过数据卷恢复"
fi

log "重启 aether"
docker compose up -d --no-build aether

log "完成。建议：1) 打开 http://<IP>:8010 确认登录与设备状态；2) 跑 python scripts/diagnose.py 体检"
