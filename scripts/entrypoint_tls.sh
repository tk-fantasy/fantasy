#!/bin/sh
# Aether 容器 TLS 入口：证书缺失时生成临时自签证书兜底，保证全新机器
# `docker compose up` 不再因 certs/ 不在 git 里而起不来。
#
# 为什么容器里生成的只是"兜底"：SAN 只能拿到容器自己可见的地址，而浏览器
# 访问的是宿主机局域网 IP——精确覆盖宿主 IP、无浏览器警告的正式证书请在
# 宿主机跑 scripts/gen_https_cert.sh（IP 变更后同样），然后重启容器。
# 宿主机已生成过的证书会原样使用，本脚本绝不覆盖。
set -eu

CERT_DIR=/aether/certs
CRT="$CERT_DIR/aether.crt"
KEY="$CERT_DIR/aether.key"

if [ ! -f "$CRT" ] || [ ! -f "$KEY" ]; then
    echo "[entrypoint] 未找到 TLS 证书，生成临时自签证书（浏览器会警告，点「继续前往」可用；"
    echo "[entrypoint] 宿主机跑 scripts/gen_https_cert.sh 生成正式证书后 docker compose restart aether）"
    mkdir -p "$CERT_DIR"
    # SAN：localhost + 容器自见地址 + Docker Desktop 宿主别名；宿主局域网 IP
    # 访问会报「证书不匹配」，仍可点继续，属预期的兜底行为。
    SAN="DNS:localhost,IP:127.0.0.1,DNS:host.docker.internal"
    for ip in $(hostname -i 2>/dev/null || true); do
        case "$ip" in
            127.*|0.0.0.0|::|::1.*) continue ;;
        esac
        SAN="$SAN,IP:$ip"
    done
    openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
        -keyout "$KEY" -out "$CRT" \
        -subj "/CN=aether-temp" \
        -addext "basicConstraints=CA:FALSE" \
        -addext "keyUsage=digitalSignature,keyEncipherment" \
        -addext "extendedKeyUsage=serverAuth" \
        -addext "subjectAltName=$SAN" >/dev/null 2>&1
    # 宿主机后续重签时可能以非 root 用户运行 gen 脚本，放开属主限制避免写不进
    chmod 666 "$CRT" "$KEY" 2>/dev/null || true
    echo "[entrypoint] 临时证书已生成（SAN: $SAN）"
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8010 \
    --ssl-certfile "$CRT" --ssl-keyfile "$KEY"
