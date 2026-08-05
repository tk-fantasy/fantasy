#!/usr/bin/env bash
# sync_ha_ip.sh —— HA 配置 IP 自动同步
#
# 问题：DHCP 分配的 WLAN IP 会变（.26 → .47 → ...），但 HA 配置里写死了 IP，
#       导致 OAuth 回调失败、xiaomi_home 授权接不进来。
#
# 本脚本：检测当前 WLAN IP，与上次记录对比，变了就自动更新：
#   1. ha_config/configuration.yaml 的 internal_url / external_url
#   2. ha_config/.storage/core.config_entries 的 xiaomi_home oauth_redirect_url
#   3. 重启 aether-ha 容器使配置生效
#
# 用法：
#   ./scripts/sync_ha_ip.sh           # 正常执行（默认）
#   ./scripts/sync_ha_ip.sh --check   # 仅检测+打印，不写文件不重启（dry-run）
#
# 部署：配合 Windows 计划任务，开机自动运行（见 scripts/install_sync_task.ps1）

set -euo pipefail

# ---------- 路径与常量 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HA_CONFIG_DIR="$REPO_ROOT/ha_config"
YAML_FILE="$HA_CONFIG_DIR/configuration.yaml"
ENTRIES_FILE="$HA_CONFIG_DIR/.storage/core.config_entries"
STATE_FILE="$HA_CONFIG_DIR/.ha_ip_state"          # 记录上次同步的 IP
HA_PORT=8123
HA_CONTAINER="aether-ha"
WLAN_IFACE="WLAN"   # netsh 接口名；如改名改这里

LOG_PREFIX="[sync_ha_ip] "
log()  { echo "${LOG_PREFIX}$*"; }
warn() { echo "${LOG_PREFIX}WARN: $*" >&2; }
die()  { echo "${LOG_PREFIX}ERROR: $*" >&2; exit 1; }

DRY_RUN=false
[[ "${1:-}" == "--check" ]] && DRY_RUN=true

# ---------- 1. 探测当前 WLAN IP ----------
# 用 netsh 精确取 WLAN 接口 IP，避免误抓 VMware / Tailscale / Hyper-V 地址。
get_wlan_ip() {
  local raw
  # 中文 Windows: "IP 地址:  192.168.x.x"
  # 英文 Windows: "IP Address:  192.168.x.x"
  # 排除子网前缀行（含 "/" 或 "掩码"/"mask"）
  raw="$(netsh interface ip show address "$WLAN_IFACE" 2>/dev/null \
        | grep -iE 'IP Address|IP 地址|IP地址' \
        | grep -viE 'subnet|prefix|前缀|掩码|mask' \
        | head -1 || true)"
  # 提取形如 192.168.x.x 的地址
  echo "$raw" | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' | head -1
}

CUR_IP="$(get_wlan_ip)"
[[ -z "$CUR_IP" ]] && die "无法获取 WLAN($WLAN_IFACE) 的 IP，脚本退出。"
log "当前 WLAN IP: $CUR_IP"

# 校验是私网地址（避免误把公网/Tailscale IP 写进内网 URL）
is_private() {
  local ip="$1"
  # 简单判定：10. / 172.16-31. / 192.168. / 100.64-127. (CGNAT/Tailscale 常见段)
  [[ "$ip" =~ ^192\.168\. ]] && return 0
  [[ "$ip" =~ ^10\. ]] && return 0
  [[ "$ip" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. ]] && return 0
  # Tailscale (100.64-127) 不算家庭内网，排除
  return 1
}
if ! is_private "$CUR_IP"; then
  die "当前 IP $CUR_IP 不是家庭私网地址 (192.168/10/172.16-31)，疑似 Tailscale/公网，拒绝写入。"
fi

# ---------- 2. 与上次记录对比 ----------
LAST_IP=""
[[ -f "$STATE_FILE" ]] && LAST_IP="$(cat "$STATE_FILE" 2>/dev/null || echo '')"

if [[ "$CUR_IP" == "$LAST_IP" ]]; then
  log "IP 未变化（仍为 $CUR_IP），无需操作。退出。"
  exit 0
fi

if $DRY_RUN; then
  log "[dry-run] IP 变化: $LAST_IP → $CUR_IP"
  log "[dry-run] 将更新: $YAML_FILE, $ENTRIES_FILE，并重启 $HA_CONTAINER"
  exit 0
fi

log "检测到 IP 变化: '${LAST_IP:-<无记录>}' → '$CUR_IP'，开始更新配置..."

# ---------- 3. 更新 configuration.yaml ----------
# 用 sed 替换 http://<任意IP>:8123 → 新 IP。匹配旧 IP 用通配，避免硬编码。
replace_ip_in_yaml() {
  local file="$1"
  # 匹配 internal_url/external_url 行里的 http://X.X.X.X:8123
  # 仅替换这两行，不动其他配置
  sed -i -E \
    -e 's#(internal_url: "http://)[0-9]{1,3}(\.[0-9]{1,3}){3}(:8123")#\1'"$CUR_IP"'\3#' \
    -e 's#(external_url: "http://)[0-9]{1,3}(\.[0-9]{1,3}){3}(:8123")#\1'"$CUR_IP"'\3#' \
    "$file"
}

replace_ip_in_yaml "$YAML_FILE"
log "  ✓ 更新 $YAML_FILE"

# ---------- 4. 更新 config_entries (xiaomi_home oauth_redirect_url) ----------
replace_ip_in_entries() {
  local file="$1"
  # 仅替换 xiaomi_home 条目里的 oauth_redirect_url（唯一含此字段的行）
  sed -i -E \
    's#("oauth_redirect_url":"http://)[0-9]{1,3}(\.[0-9]{1,3}){3}(:8123/api/webhook/[^"]*")#\1'"$CUR_IP"'\3#' \
    "$file"
}
replace_ip_in_entries "$ENTRIES_FILE"
log "  ✓ 更新 $ENTRIES_FILE (oauth_redirect_url)"

# ---------- 5. 写状态文件 ----------
echo "$CUR_IP" > "$STATE_FILE"
log "  ✓ 写状态文件 $STATE_FILE"

# ---------- 6. 重启 HA ----------
if docker ps --format '{{.Names}}' | grep -q "^${HA_CONTAINER}$"; then
  log "  → 重启容器 $HA_CONTAINER ..."
  docker restart "$HA_CONTAINER" >/dev/null 2>&1 || { warn "docker restart 失败，请手动重启"; }
  # 等待 HA 就绪
  for i in $(seq 1 15); do
    if curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://localhost:$HA_PORT/" 2>/dev/null | grep -q 200; then
      log "  ✓ HA 已就绪（第 $i 次探测）"
      break
    fi
    sleep 3
  done
else
  warn "容器 $HA_CONTAINER 未在运行，跳过重启。"
fi

log "完成。$LAST_IP → $CUR_IP，配置已同步。"
log "提示：xiaomi_home 若仍授权失败，需在 HA 界面「设置 → 设备与服务 → Xiaomi Home → 重新认证」走一次 OAuth。"
