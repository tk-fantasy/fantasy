#!/usr/bin/env bash
# Aether 备份脚本（09 清单条目 6）—— 树莓派上运行
#
# 打包 config.json + .env + HA 配置 + MQTT 配置 + 数据库卷（用户/会话/个性化），
# 保留最近 3 份。建议配合 cron 每日运行：
#   0 4 * * * cd /path/to/Aether && ./scripts/backup.sh
set -euo pipefail

BACKUP_DIR="backups"
KEEP=3

log() { echo "[backup] $*"; }

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/aether-backup-$TS.tar.gz"

# 数据卷名：compose 项目前缀 + aether-data
DATA_VOLUME=$(docker volume ls --format '{{.Name}}' | grep -E 'aether-data$' | head -1 || true)

# 宿主侧文件（存在才打包，容忍部分缺失）
HOST_FILES=()
for f in config.json .env mosquitto/mosquitto.conf mosquitto/init.sh; do
  [ -f "$f" ] && HOST_FILES+=("$f")
done
# 注意：gzip 包不能 append（tar rf 只支持未压缩归档），先打裸 tar，追加完再压缩
RAW="$BACKUP_DIR/aether-backup-$TS.tar"
if [ -d ha_config ]; then
  tar cf "$RAW" "${HOST_FILES[@]}" \
    --exclude='ha_config/home-assistant_v2.db*' \
    --exclude='ha_config/home-assistant.log*' \
    --exclude='ha_config/.ha_run.lock' \
    --exclude='ha_config/.simulator.lock' \
    --exclude='ha_config/custom_components' \
    --exclude='ha_config/.storage' \
    ha_config
else
  tar cf "$RAW" "${HOST_FILES[@]}"
fi

# 数据库卷（用户/聊天记录/个性化设置/实体映射都在里面）追加进同一包
if [ -n "$DATA_VOLUME" ]; then
  docker run --rm -v "$DATA_VOLUME":/data -v "$(pwd)/$RAW":/backup.tar alpine \
    sh -c "tar rf /backup.tar -C /data --transform 's,^\.,data,' ."
  log "已包含数据卷 $DATA_VOLUME"
fi

gzip -9 "$RAW"
OUT="$BACKUP_DIR/aether-backup-$TS.tar.gz"

# 只保留最近 KEEP 份
ls -1t "$BACKUP_DIR"/aether-backup-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  rm -f "$old" && log "清理旧备份: $old"
done

log "完成: $OUT ($(du -h "$OUT" | cut -f1))"
log "恢复: ./scripts/restore.sh $OUT"
