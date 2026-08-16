#!/bin/bash
# git 一键升级容器入口。由 app/ops/git_update.py 经 Docker API 拉起，环境变量：
#   MODE=check|apply      check=只 fetch 比对；apply=执行 scripts/update-from-git.sh
#   GIT_TOKEN             Gitee 私有令牌（HTTPS 拉取认证；check/apply 都需要）
#   RESULT_FILE           结果 JSON 写入路径（挂载到 aether 的 logs/ 下）
#   LOG_FILE              apply 的完整日志路径
#   HEALTH_URL            apply 的健康自检地址（host.docker.internal 指回宿主）
#   HOME=/tmp             避免 git 报 dubious ownership / 权限问题
set -uo pipefail   # 不用 -e：失败也要写出结果 JSON，由状态接口展示

MODE="${MODE:-check}"
RESULT_FILE="${RESULT_FILE:-/result/git-update-result.json}"
LOG_FILE="${RESULT_FILE%/*}/git-update.log"
HEALTH_URL="${HEALTH_URL:-http://host.docker.internal:8010/api/health}"
export HOME=/tmp
export GIT_TERMINAL_PROMPT=0

# 挂载进来的仓库属于宿主用户（UID 与容器内 root 不同），
# 不声明 safe.directory git 会以 dubious ownership 拒绝所有操作
git config --global --add safe.directory /repo
# 宿主为 Windows 开发机时工作树是 CRLF，容器内 git 默认不转换会误报"工作树脏"；
# input = 比对时按 LF 归一，对 Linux 部署（文件本就是 LF）无影响
git config --global core.autocrlf input

# compose 项目名必须与要升级的现有栈一致：否则 /repo 目录名推导出项目 "repo"，
# 会另起一套容器（端口冲突）而不是升级现有 aether 栈。从运行中容器的 label 读真实项目名。
PROJECT=$(docker inspect aether --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null | tr -d '\r\n')
[ -n "$PROJECT" ] && export COMPOSE_PROJECT_NAME="$PROJECT"

# 令牌经 GIT_ASKPASS 注入（不进命令行、不进 git config，进程列表与配置文件都看不到）
ASKPASS="$(mktemp)"
cat > "$ASKPASS" <<'EOF'
#!/bin/sh
echo "${GIT_TOKEN:-}"
EOF
chmod +x "$ASKPASS"
trap 'rm -f "$ASKPASS"' EXIT
export GIT_ASKPASS="$ASKPASS"
export GIT_ASKPASS_REQUIRE=force    # force=只信 askpass，不走 ssh/cached 凭证

mkdir -p "$(dirname "$RESULT_FILE")"

# origin 是 SSH 形式时改写为 HTTPS（令牌认证走 HTTPS）；只在 remote 确为 SSH 时执行
origin=$(git remote get-url origin 2>/dev/null || true)
if [ -n "$origin" ] && [[ "$origin" == git@* || "$origin" == ssh://* ]]; then
  host=$(echo "$origin" | sed -E 's#^(git@|ssh://)##; s#[:/]#. #' | awk '{print $1}')
  path=$(echo "$origin" | sed -E 's#^(git@|ssh://)##; s#^[^:/]*[:/]##')
  git remote set-url origin "https://${host}/${path}" || true
fi

# ---------- MODE=check：fetch + 比对 ----------
if [ "$MODE" = "check" ]; then
  if ! git fetch origin 2>>"$LOG_FILE"; then
    jq -n --arg e "git fetch 失败（令牌无效/网络不通/仓库路径不对），详见日志" \
      '{status:"error", message:$e}' > "$RESULT_FILE"
    exit 0
  fi
  head_sha=$(git rev-parse HEAD)
  # upstream 优先；老仓库（push 而非 clone 建立）没有 origin/HEAD，退回 origin/master
  up_sha=$(git rev-parse @{u} 2>/dev/null || git rev-parse origin/master 2>/dev/null || git rev-parse origin/HEAD)
  if [ "$head_sha" = "$up_sha" ]; then
    jq -n --arg c "$(git rev-parse --short HEAD)" \
      '{status:"up_to_date", current_commit:$c}' > "$RESULT_FILE"
  else
    behind=$(git rev-list --count HEAD.."$up_sha")
    jq -n --arg c "$(git rev-parse --short HEAD)" --arg r "$(git rev-parse --short "$up_sha")" \
      --argjson b "$behind" \
      '{status:"available", current_commit:$c, remote_commit:$r, behind:$b}' > "$RESULT_FILE"
  fi
  exit 0
fi

# ---------- MODE=apply：完整升级（脚本自带干净树检查/ff-only/健康自检/失败回退） ----------
export HEALTH_URL   # update-from-git.sh 读取
echo "[git-updater] $(date '+%F %T') 开始 git 升级" > "$LOG_FILE"
if bash scripts/update-from-git.sh >>"$LOG_FILE" 2>&1; then
  # 成功：把脚本解析的版本信息补进结果（脚本已把记录写进 backups/upgrade-history.jsonl）
  new_v=$(python3 -c 'import json;print(json.load(open("version.json"))["version"])' 2>/dev/null || echo unknown)
  jq -n --arg v "$new_v" '{status:"success", to_version:$v}' > "$RESULT_FILE"
else
  rc=$?
  jq -n --argjson rc "$rc" \
    '{status:"failed", message:"升级失败（脚本已自动回退或中止），详见日志", exit_code:$rc}' > "$RESULT_FILE"
fi
