#!/usr/bin/env bash
# Aether 离线升级脚本（09 清单条目 2）—— 客户侧（树莓派）命令行版
#
# 命令行版离线升级（走 docker CLI），适合远程 SSH 操作或应用起不来的场景，
# 与运维页在线更新源通道互为补充。
#
# 用法：把升级包拷到仓库目录（含 docker-compose.yml 的目录），执行：
#     ./scripts/upgrade.sh aether-update-1.0.1.tar.gz
#
# 流程：校验包 → 版本兼容检查 → 备份（config/.env/数据卷）
#       → docker load → tag latest → up -d → 健康自检 → 失败自动回滚
# 说明：数据库 schema 迁移由应用启动时自完成（_ensure_column 机制）。
set -euo pipefail

BACKUP_DIR="backups"
HEALTH_URL="http://127.0.0.1:8010/api/health"
HEALTH_TIMEOUT=180

log()  { echo "[upgrade] $*"; }
die()  { echo "[upgrade][错误] $*" >&2; exit 1; }

# ---------- 0. 预检 ----------
[ $# -eq 1 ] || die "用法: ./upgrade.sh aether-update-<版本>.tar.gz"
PACK="$1"
[ -f "$PACK" ] || die "升级包不存在: $PACK"
command -v docker >/dev/null || die "未安装 docker"
[ -f docker-compose.yml ] || die "请在含 docker-compose.yml 的仓库目录运行"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# ---------- 1. 解包 + 完整性校验 ----------
log "解包 $PACK"
tar xzf "$PACK" -C "$WORK"
[ -f "$WORK/manifest.json" ] || die "包内缺 manifest.json（非本工具产出的包？）"

sha_expect=$(python3 -c "import json;print(json.load(open('$WORK/manifest.json'))['images'][0]['sha256'])")
sha_actual=$(sha256sum "$WORK/images/aether.tar" | cut -d' ' -f1)
[ "$sha_expect" = "$sha_actual" ] || die "镜像校验失败（sha256 不匹配，包可能损坏）"

# ---------- 2. 版本兼容检查 ----------
python3 - "$WORK/manifest.json" <<'EOF' || die "版本不兼容，中止升级（请联系支持获取迁移指引）"
import json, sys
new = json.load(open(sys.argv[1]))
try:
    cur = json.load(open("version.json"))
except (OSError, ValueError):
    sys.exit(0)  # 老部署没有 version.json，视为可升级
def key(v):
    return tuple(int(x) if x.isdigit() else 0 for x in v.split("."))
if key(new.get("min_compatible", new["version"])) > key(cur.get("version", "0")):
    print(f"当前 {cur.get('version')} 低于升级包要求的最低兼容版本 {new['min_compatible']}")
    sys.exit(1)
EOF

NEW_VERSION=$(python3 -c "import json;print(json.load(open('$WORK/manifest.json'))['version'])")
log "升级包版本: $NEW_VERSION"

# ---------- 3. 备份（升级失败回滚的依据；与应用侧备份同目录同格式） ----------
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/pre-upgrade-$TS.tar.gz"
log "备份到 $BACKUP"
DATA_VOLUME=$(docker volume ls --format '{{.Name}}' | grep -E 'aether-data$' | head -1 || true)
# gzip 包不能 append：先打裸 tar，追加卷数据后再压缩
RAW="$BACKUP_DIR/pre-upgrade-$TS.tar"
tar cf "$RAW" config.json .env 2>/dev/null || tar cf "$RAW" config.json || true
if [ -n "$DATA_VOLUME" ]; then
  docker run --rm -v "$DATA_VOLUME":/data -v "$(pwd)/$RAW":/backup.tar alpine \
    sh -c "tar rf /backup.tar -C /data --transform 's,^\.,data,' ."
fi
[ -f "$RAW" ] || die "备份失败，中止（宁可不动也不能丢数据）"
gzip -9 "$RAW"

# 记录当前版本，回滚用
OLD_VERSION=$(python3 -c "import json;print(json.load(open('version.json'))['version'])" 2>/dev/null || echo unknown)
log "当前版本: $OLD_VERSION"

rollback() {
  log "升级失败，回滚到 $OLD_VERSION"
  if [ "$OLD_VERSION" != "unknown" ] && docker image inspect "aether-app:$OLD_VERSION" >/dev/null 2>&1; then
    docker tag "aether-app:$OLD_VERSION" aether-app:latest
    docker compose up -d --no-build aether >/dev/null 2>&1 || docker restart aether || true
  fi
  log "备份保留在 $BACKUP（如需手工恢复数据卷：docker run --rm -v <卷>:/data -v $BACKUP:/backup.tar.gz alpine sh -c 'cd /data && tar xzf /backup.tar.gz data --strip-components=1'）"
  die "已回滚。请把 $BACKUP 与 logs/ 发给支持人员"
}

# ---------- 4. 载入镜像并切到 latest（compose 固定引用 latest，无需改文件） ----------
log "docker load 新镜像"
docker load -i "$WORK/images/aether.tar"
log "tag → aether-app:latest"
docker tag "aether-app:$NEW_VERSION" aether-app:latest

# ---------- 5. 起服 + 健康自检 ----------
log "重启 aether（应用新镜像）"
docker restart aether || docker compose up -d --no-build aether || rollback

log "等待服务就绪（最长 ${HEALTH_TIMEOUT}s）"
elapsed=0
while [ "$elapsed" -lt "$HEALTH_TIMEOUT" ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HEALTH_URL" || echo 000)
  # 任何 HTTP 应答（含 401 未认证）都证明服务活着；000=连接失败
  if [ "$code" != "000" ] && [ "$code" != "" ]; then
    log "服务已就绪（HTTP $code）"
    log "升级成功 $OLD_VERSION → $NEW_VERSION。旧备份: $BACKUP"
    python3 - "$OLD_VERSION" "$NEW_VERSION" <<'EOF'
import json, sys
v = json.load(open("version.json")); v["version"] = sys.argv[2]
json.dump(v, open("version.json", "w"), ensure_ascii=False, indent=2)
EOF
    exit 0
  fi
  sleep 5; elapsed=$((elapsed + 5))
done
rollback
