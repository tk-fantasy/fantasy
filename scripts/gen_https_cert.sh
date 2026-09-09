#!/usr/bin/env bash
# 生成 Aether 自签 HTTPS 证书：本地根 CA + 服务端叶子证书。
#
# 用法：
#   scripts/gen_https_cert.sh                              # 自动探测本机所有 IPv4（含 Tailscale）写入 SAN
#   scripts/gen_https_cert.sh 10.0.0.5 my.home.example     # 追加额外的 IP / 域名
#
# 说明：
# - CA 只在缺失时创建（旧 CA 保留，设备导入一次即可长期信任）；叶子证书每次运行重签——
#   路由器重新分配 IP 后，重跑本脚本再重启容器即可，设备端无需重新导入。
# - 叶子证书有效期 825 天（iOS 对手动信任证书的叶子期限上限），CA 有效期 10 年。
# - 产物在 certs/（已 .gitignore 排除，私钥绝不入库）：
#     rootCA.crt  → 导入各设备的受信任根证书存储
#     aether.crt / aether.key → docker-compose 挂载给 uvicorn --ssl-certfile/--ssl-keyfile
set -euo pipefail
cd "$(dirname "$0")/.."

# Git Bash/MSYS 会把以 / 开头的参数（如 -subj "/CN=..."）改写成 Windows 路径，
# 必须禁用路径转换；Linux 下这两个变量无副作用。
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

CERT_DIR="certs"
CA_KEY="$CERT_DIR/rootCA.key"
CA_CRT="$CERT_DIR/rootCA.crt"
LEAF_KEY="$CERT_DIR/aether.key"
LEAF_CRT="$CERT_DIR/aether.crt"
LEAF_CSR="$CERT_DIR/aether.csr"
mkdir -p "$CERT_DIR"

# ── 收集 SAN：localhost + 本机全部 IPv4（去重，排除回环/链路本地/子网掩码）+ Tailscale ──
ip_list="$(
  {
    ipconfig 2>/dev/null | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' || true
    hostname -I 2>/dev/null || true
    tailscale ip -4 2>/dev/null || true
  } | tr ' \t' '\n\n' | sort -u
)"

SAN="DNS:localhost,IP:127.0.0.1"
while IFS= read -r ip; do
  [ -z "$ip" ] && continue
  case "$ip" in
    127.*|0.0.0.0|169.254.*|255.*) continue ;;  # 回环 / 未指定 / 链路本地 / 子网掩码
  esac
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
  SAN="$SAN,IP:$ip"
done <<< "$ip_list"
# 命令行追加条目（IPv4 走 IP:，其余按域名处理）
for extra in "$@"; do
  if [[ "$extra" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="$SAN,IP:$extra"
  else
    SAN="$SAN,DNS:$extra"
  fi
done

# ── 根 CA（存在则复用）──
if [ ! -f "$CA_CRT" ] || [ ! -f "$CA_KEY" ]; then
  echo "[cert] 生成新的本地根 CA（请把 $CA_CRT 导入常用设备，见 docs/tech/HTTPS部署指南.md）"
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$CA_KEY" -out "$CA_CRT" \
    -subj "/CN=Aether Local Root CA/O=Aether" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
fi

# ── 服务端叶子证书（每次重签）──
echo "[cert] SAN: $SAN"
openssl req -newkey rsa:2048 -sha256 -nodes \
  -keyout "$LEAF_KEY" -out "$LEAF_CSR" \
  -subj "/CN=aether-local" >/dev/null 2>&1
# 扩展项写真实文件而非进程替换：MSYS 对 /dev/fd/* 路径的处理不可靠
EXT_FILE="$CERT_DIR/aether.ext"
{
  echo "basicConstraints=CA:FALSE"
  echo "keyUsage=critical,digitalSignature,keyEncipherment"
  echo "extendedKeyUsage=serverAuth"
  echo "subjectAltName=$SAN"
} > "$EXT_FILE"
openssl x509 -req -sha256 -days 825 \
  -in "$LEAF_CSR" -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$LEAF_CRT" -extfile "$EXT_FILE"
rm -f "$LEAF_CSR" "$EXT_FILE" "$CERT_DIR/rootCA.srl"
chmod 600 "$LEAF_KEY" "$CA_KEY" 2>/dev/null || true

echo "[cert] 完成：aether.crt + aether.key（服务端用）、rootCA.crt（导入设备用）"
