# HTTPS 自签证书部署指南

> 2026-09 起，Aether 主服务（8010 端口）默认启用 HTTPS，由 uvicorn 直接终结 TLS，
> 无需反向代理。本文说明证书的生成、设备信任配置、常见坑与回滚方法。

## 现状与架构

- **终结点**：uvicorn 启动参数 `--ssl-certfile/--ssl-keyfile`（由 `scripts/entrypoint_tls.sh`
  作为容器入口拼装），8010 端口**只讲 HTTPS**，不存在 HTTP→HTTPS 自动跳转。
- **证书自动兜底**：容器启动时若发现 `certs/` 缺失（全新 clone 不含该目录），会自动生成
  一张**临时自签证书**（CN=aether-temp），保证 `docker compose up` 直接能起、能访问；
  浏览器访问宿主局域网 IP 会报「证书不匹配」，点「继续前往」仍可用。已有证书则原样使用，绝不覆盖。
- **正式证书**：宿主机跑 `scripts/gen_https_cert.sh` 生成（本地 CA + 叶子证书，SAN 自动覆盖
  本机所有 IPv4 含 Tailscale 网段 + localhost），跑完 `docker compose restart aether` 即无警告。
- **协议**：HTTP/1.1（uvicorn 未装 h2 包，ALPN 只提供 http/1.1；WebSocket 同样走 1.1 升级）。
  局域网延迟下 HTTP/2 无收益，不影响体验。
- **性能**：TLS 握手每连接多一次往返（长连接/keep-alive 下一次性成本）；对称加密有
  AES-NI 硬件加速，家用流量规模下 CPU 开销可忽略。
- **证书挂载**：`./certs:/aether/certs:ro`；整目录已被 `.gitignore` 排除，**私钥绝不入库**。

## 访问方式变化（重要）

- 一律使用 `https://`，如 `https://<局域网IP>:8010`、`https://<Tailscale IP>:8010`（Tailscale）。
  **旧的 `http://` 地址会直接连接失败**（同一端口无法同时讲两种协议）。
- 浏览器打开 `/` 会 307 跳转到 `/landing`——这是 `app/routes/setup_routes.py` 的原有路由设计，与 HTTPS 无关。
- Cookie 的 `Secure` 标志自动跟随（`app/core/auth.py` 的 `is_secure_request`），无需配置。

## 证书管理

```bash
# 生成/重签正式证书（CA 缺失时才创建；叶子证书每次重签）
scripts/gen_https_cert.sh

# 追加额外 IP 或域名到 SAN（如换了路由器网段、加了内网 DNS 名）
scripts/gen_https_cert.sh 10.0.0.5 aether.home.example

# IP 变更后的完整流程：重签 → 重启容器
scripts/gen_https_cert.sh && docker compose restart aether
```

要点：

- **全新部署零手工**：不跑任何证书命令，`docker compose up -d` 也能起——容器自动生成
  临时证书兜底（浏览器有警告，可点继续）。推荐跑一次生成脚本换正式证书。
- **CA 复用**：只要 `certs/rootCA.crt` 存在就不重新生成，设备导入一次长期有效；
  IP 变更只重签叶子证书，设备端零操作。
- **有效期**：叶子 825 天（iOS 对手动信任证书的叶子期限上限），CA 10 年。
- 换网络环境后新 IP 不在 SAN 里的话，浏览器报的是「证书不匹配」（仍可点继续），
  跑一次重签即恢复无警告。
- Linux 宿主机上若 `certs/` 由容器自动创建（root 属主），宿主机重签时用 `sudo`。

## 设备信任配置（消除浏览器警告）

未导入 CA 的设备首次访问会出现「您的连接不是私密连接」，点 **高级 → 继续前往**
即可正常使用（Chrome/Edge/Safari 均有此入口）。要永久无警告，按设备导入根 CA：

### Windows（部署机已导入，当前用户信任库）

```powershell
certutil -user -addstore Root certs\rootCA.crt     # 导入
certutil -user -delstore Root "Aether Local Root CA"  # 删除（回滚时）
```

### iPhone / iPad

1. 把 `certs/rootCA.crt` 发到手机（隔空投送 / 微信文件 / AirDrop 均可），在「文件」里点开
2. 设置 → 通用 → VPN 与设备管理 → 安装描述文件
3. **关键一步**：设置 → 通用 → 关于本机 → 证书信任设置 → 给「Aether Local Root CA」打开完全信任
   （不打开这步 Safari 仍会警告）

### Android

设置 → 安全 → 更多安全设置 → 加密与凭据 → 安装证书 → CA 证书（各厂商路径略有差异），
选中 `rootCA.crt` 安装。Chrome 随后即信任。

### 其他设备（电视、老平板等）

无法导入 CA 的设备只能每次点「继续前往」；个别老旧设备的内置浏览器连 TLS 版本都过新
支持不了，属设备限制。

## 命令行访问注意事项

- **Windows curl（Git Bash / 系统 curl）**：走 Schannel，默认强制吊销检查，而自签 CA
  没有 CRL 吊销点，会报 `CRYPT_E_NO_REVOCATION_CHECK (0x80092012)`。加
  `--ssl-no-revoke` 即可（CA 导入信任库后配合使用，实现完整校验）：
  ```bash
  curl -s --ssl-no-revoke https://127.0.0.1:8010/healthz
  ```
  浏览器**不受吊销检查影响**（Chrome/Edge 对吊销是软失败策略）。Linux（树莓派）的
  curl 用 OpenSSL 后端，默认不做吊销检查，直接用。
- **仓库内运维脚本已适配**：`upgrade.sh` / `update-from-git.sh` 健康探测改为
  `https:// + curl -k`；compose healthcheck 改为 https + 跳过自签校验。
- **开发工具**：`scripts/loadtest_ws*.py` 默认 `ws://127.0.0.1:8010`，如需对 TLS 端口
  压测，用环境变量 `AETHER_WS_URL=wss://...` / `AETHER_API_URL=https://...` 覆盖，
  websocket/httpx 客户端需带跳过证书校验的 ssl 参数。
- **本地开发模式（不走 Docker）**：`python -m uvicorn ...` 仍是 HTTP；要 TLS 就在
  命令后追加 `--ssl-certfile certs/aether.crt --ssl-keyfile certs/aether.key`。

## 本次改动清单

| 文件 | 改动 |
| --- | --- |
| `scripts/gen_https_cert.sh` | 新增：宿主机生成本地 CA + 叶子证书（SAN 自动探测） |
| `scripts/entrypoint_tls.sh` | 新增：容器 TLS 入口，证书缺失时自动生成临时兜底证书 |
| `docker-compose.yml` | `entrypoint:` 挂载入口脚本；挂载 `./certs`（可写）；healthcheck 改 https |
| `scripts/upgrade.sh` / `update-from-git.sh` | `HEALTH_URL` 改 https，curl 加 `-k` |
| `scripts/restore.sh` | 提示语改 https |
| `.gitignore` | 排除 `certs/` |
| `README.md` / `README.en.md` | 访问地址改 https，补证书警告说明 |

## 回滚到 HTTP

删掉 `docker-compose.yml` 里 aether 服务的 `entrypoint:` 覆盖与 `./certs`、入口脚本两处挂载，然后：

```bash
docker compose up -d aether
```

即恢复原 HTTP 部署（镜像未变，无需 rebuild）。
