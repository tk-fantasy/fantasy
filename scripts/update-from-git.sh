#!/usr/bin/env bash
# Aether git 直更脚本（自用流）—— 拉取最新代码并现场重建。
#
# 用法（仓库根目录）：./scripts/update-from-git.sh
# 前提：私库已配好只读部署公钥（Gitee/GitHub 仓库级 Deploy Key，见 docs）
# 流程：干净树检查 → pull → 重建 → 健康自检 → 失败自动回退上一 commit 重建
# 说明：升级包通道（upgrade.sh / 运维页）面向交付场景；本脚本面向开发者自用，
#       两者互不干扰。升级记录同样写入 backups/upgrade-history.jsonl，
#       运维页「升级历史」能看到 git 更新。
set -euo pipefail

HEALTH_URL="http://127.0.0.1:8010/api/health"
HEALTH_TIMEOUT=240   # 现场构建后首次启动含 RAG 索引等，放宽

log()  { echo "[git-update] $*"; }
die()  { echo "[git-update][错误] $*" >&2; exit 1; }

command -v git >/dev/null || die "未安装 git"
[ -f docker-compose.yml ] || die "请在含 docker-compose.yml 的仓库目录运行"

# ---------- 0. 工作树必须干净（本地改动会跟更新打架）----------
if [ -n "$(git status --porcelain)" ]; then
  die "工作树有未提交改动，请先 commit/stash 再更新"
fi
OLD_COMMIT=$(git rev-parse --short HEAD)
log "当前 commit: $OLD_COMMIT"

# ---------- 1. 拉取（只接受 fast-forward，历史分叉则中止）----------
git fetch origin || die "git fetch 失败（检查部署公钥/网络）"
HEAD_TARGET=$(git rev-parse @{u} 2>/dev/null || git rev-parse origin/HEAD)
if [ "$(git rev-parse HEAD)" = "$HEAD_TARGET" ]; then
  log "已是最新（$OLD_COMMIT），无需更新"
  exit 0
fi
git pull --ff-only || die "pull 失败（非快进分叉？请人工处理）"
NEW_COMMIT=$(git rev-parse --short HEAD)
log "更新: $OLD_COMMIT → $NEW_COMMIT，开始重建（Pi 上约几分钟）"

# ---------- 2. 重建并起服 ----------
docker compose up -d --build aether || die "构建/启动失败，见上方日志"

# ---------- 3. 健康自检 ----------
log "等待服务就绪（最长 ${HEALTH_TIMEOUT}s）"
elapsed=0
ok=0
while [ "$elapsed" -lt "$HEALTH_TIMEOUT" ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HEALTH_URL" || echo 000)
  if [ "$code" != "000" ] && [ "$code" != "" ]; then
    ok=1; break
  fi
  sleep 5; elapsed=$((elapsed + 5))
done

# ---------- 4. 记录升级历史（运维页「升级历史」可见）----------
_ver_of() {
  git show "$1:version.json" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || echo unknown
}
FROM_V=$(_ver_of "$OLD_COMMIT"); TO_V=$(_ver_of HEAD)
mkdir -p backups
python3 - "$FROM_V" "$TO_V" "$OLD_COMMIT" "$NEW_COMMIT" "$ok" <<'EOF'
import datetime, json, sys
from_v, to_v, old_c, new_c, ok = sys.argv[1:6]
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "operator": "git",
    "from_version": from_v,
    "to_version": to_v,
    "notes": f"git pull + rebuild（{old_c} → {new_c}）{'成功' if ok == '1' else '健康检查未过，已回退'}",
}
with open("backups/upgrade-history.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
EOF

# ---------- 5. 失败回退 ----------
if [ "$ok" = "1" ]; then
  log "更新成功: $OLD_COMMIT → $NEW_COMMIT（v$FROM_V → v$TO_V）"
  exit 0
fi

log "健康检查超时，回退到 $OLD_COMMIT 并重建"
git reset --hard "$OLD_COMMIT"
docker compose up -d --build aether || true
die "已回退到 $OLD_COMMIT。请查 logs/ 排障（或跑 python scripts/diagnose.py）"
