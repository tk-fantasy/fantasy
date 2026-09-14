# Docker 服务部署指南

这篇讲怎么用 Docker 把 Aether 跑起来。

## 四个服务

Aether 编排四个容器，都定义在 `docker-compose.yml` 里：

1. **aether**——后端主服务（API + WebSocket + 前端页面），Dockerfile 构建
2. **Home Assistant（智能家居大脑）**——管理所有智能设备，Aether 通过它的 API 控制全屋
3. **Mosquitto MQTT（消息中转）**——一个"消息邮局"，虚拟设备把状态发到这里，HA 从这里接收
4. **aether-simulator（虚拟设备模拟器）**——**默认不启动**（生产环境不该有假设备）。首次启用：`docker compose --profile simulator up -d simulator`，之后可在「高级」页一键启停；接真实设备后建议保持关闭

> 另有**可选的第五个服务 Ollama**（本地模型，默认不启动）：`docker compose --profile local-llm up -d ollama` 启动，用于纯内网零出网部署，详见《本地 Ollama 模型部署》。
>
> 全部容器 `restart: unless-stopped`——宿主机重启或容器被 OOM 杀掉后自动拉起。
>
> 老版本里还有 SearXNG 搜索引擎，现在已经换成云端的 **Exa MCP** 搜索（不占本地端口、不用本地容器）。

## 开始之前

确认三件事：

- 装了 **Docker Desktop**（Windows 版）
- Docker 在运行（任务栏图标是绿色）
- 终端能跑 `docker --version`

## 关于 HA 镜像

Aether 用官方镜像 `homeassistant/home-assistant:stable`，配置通过 `./ha_config` 挂载进去。只用了 `default_config` + 内置 MQTT 集成，没有自定义组件，`docker compose up -d` 自动拉取，无需手动构建。

## 启动服务

### 首次部署前：准备 .env 与 config.json（缺一不可）

```powershell
Copy-Item .env.example .env
Copy-Item config.example.json config.json
```

`.env` 里**必须设置 `MQTT_PASSWORD`（≥8 位强密码）**——mosquitto 初始化脚本检测到缺失会直接拒绝启动（这是防弱口令暴露设备控制面的硬门槛）。建议同时设置 `JWT_SECRET`：不设则每次重启随机生成，重启后所有登录会话失效。其余项按注释按需填写。

### 启动

在项目根目录运行：

```powershell
docker compose up -d --build
```

第一次启动会拉取 mosquitto、HA、构建 Aether 镜像（前端 npm ci + vite build + 后端 pip install），约 5–10 分钟。之后启动很快。

### 日常更新：改了代码 / 拉了新提交后

前端与后端代码都**烧在 aether 镜像里**（不挂载源码目录），所以改完代码或 `git pull` 后必须**重建镜像并滚动重建容器**，仅 `docker compose restart` 不会加载任何新代码：

```powershell
docker compose build aether
docker compose up -d aether
```

构建有层缓存，通常只重跑变化的部分（改前端≈几十秒；改了 `requirements.txt` 会重装依赖，稍久）。

### 看看是不是跑起来了

```powershell
docker compose ps
```

常用容器状态都是 `Up` 就搞定了（`ollama`/`aether-simulator` 走 profile，默认不启动）：

| 容器名 | 镜像 | 端口 |
|--------|------|------|
| `aether` | 本地构建 | 8010→8010, 8011→8011 |
| `aether-ha` | `homeassistant/home-assistant:stable` | 8123→8123 |
| `mosquitto` | `eclipse-mosquitto:2` | 127.0.0.1:1884→1884（仅回环） |
| `aether-simulator` | `python:3.11-slim` | —（`--profile simulator` 启用） |

> 小提示：日常启动只需 `docker compose up -d`，会自动起全部常驻服务（mqtt / homeassistant / aether）。
>
> `aether` 容器自带两道自愈保险：**存活探针**（`/healthz` 免认证，事件循环卡死时探活失败，Docker 自动重启）和 **2GB 内存上限**（防失控拖垮同宿主容器）。MQTT 的 1884 端口只绑定宿主回环，局域网其他机器访问不到——HA 和模拟器走 docker 内部网络，不受影响。

## 停止服务

```powershell
# 停掉容器，数据还在
docker compose down

# 停掉容器并清掉数据卷（恢复出厂设置）
docker compose down -v
```

> 用 `-v` 会删容器内数据卷，但你改的本地配置文件（`ha_config/`、`mosquitto/config/`）不会丢。

## 注册与邀请码（Aether 账号）

Aether 的注册是**默认关闭**的，两级码制：

**首次部署——安装码**

数据库里还没有任何用户时，容器启动会自动生成一枚安装码，并醒目打印到部署日志：

```powershell
docker compose logs aether | Select-String "安装码" -Context 0,3
# 首次部署安装码: XXXX-XXXX
```

也可以打开启动进度页 `http://localhost:8011/progress`（仅本机可访问）：页面大字展示安装码并附二维码，微信/相机扫码即可读码。然后打开 `https://localhost:8010` 注册，表单里填这枚码——**第一个注册的用户自动成为管理员**。安装码只在没有用户时生效（户主注册完成后此码与页面一起失效，进度页转而显示「已完成初始化」）；怀疑泄露可在管理员登录后调 `POST /api/auth/setup-code/regenerate` 重置。

**给家人开通——邀请码**

之后的新账号一律凭管理员签发的一次性邀请码注册：**运维中心（/operations）→ 注册邀请码** → 生成。列表中每枚未使用的码旁有「二维码」按钮，弹出的大图二维码内容是注册深链（`https://你的局域网IP:8010/login?mode=register&code=X`）——家人用手机扫码点开即落在注册页、码已自动填好，只需再填用户名密码。注意：自己要用**局域网 IP** 访问运维页再生成二维码（localhost 地址手机打不开，页面会提示）。邀请码**用一次即作废**，签发后 **24 小时未用自动过期**（过期与无效同一句报错，不给探测者侧信道），可随时吊销。

> 为什么这么严：Aether 首用户即管理员，而管理员可以上传插件、修改运维配置。如果注册完全敞开，新部署到你自己注册之间的窗口期里，局域网内任何人都可能抢先注册成管理员。安装码就是用来堵这个窗口的。

### HTTPS 证书与「扫码提示隐私风险」

8010 从安全加固起只讲 HTTPS，证书由 `scripts/gen_https_cert.sh` 生成的**本地自签 CA** 签发（`certs/rootCA.crt` 根证书 + `aether.crt` 叶子，均不入库）。手机/电脑没有导入这套 CA 前，访问会看到「您的连接不是私密连接」；**微信内置浏览器最严格**，扫码进来会直接红字提示「该网站存在隐私泄露风险」——这是证书不受信任的正常表现，不是数据会被偷。

处理方式（按推荐顺序）：

1. **给常用设备导入根证书（既定方案，一次管十年）**：把 `certs/rootCA.crt` 发给家人安装——Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书；iOS：用 Safari/文件打开描述文件安装后，再到「通用 → 关于本机 → 证书信任设置」开启完全信任。装完扫码/访问零警告。路由器换 IP 后重跑 `scripts/gen_https_cert.sh`（自动把本机所有 IPv4 写进 SAN）并 `docker compose up -d aether`，设备端无需重装。
2. **临时绕过**：微信里点「···」→「在浏览器中打开」，系统浏览器点「高级 → 继续前往」。能用，但每台设备第一次都会被红页吓一下。
3. **彻底方案**：申请域名 + DDNS 指向家里，用 Let's Encrypt 签发正式证书替换 `certs/aether.crt`/`aether.key`（DNS 验证无需开公网端口），所有设备零警告。


## 打开智能家居管理界面

浏览器访问：

```
http://localhost:8123
```

### 第一次进 HA 要做的事

- 如果是新启动的数据卷，HA 会让你创建管理员账号——随便填，这是 HA 自己的账号，和 Aether 的登录账号没关系
- 全新安装默认没有虚拟设备（模拟器默认关闭，见下节）；若已启用模拟器，应能看到灯/空调等演示设备
- 接入真实设备后确认列表不空，说明 MQTT 链路通了

## 虚拟设备模拟器

Aether 自带一个虚拟设备模拟器 `ha_config/ha_simulator.py`，它通过 MQTT 往 HA 报告虚拟设备状态（灯、空调、窗帘、传感器等），让没有真实硬件也能演示。

```powershell
conda run -n yolo python ha_config\ha_simulator.py
```

Docker 部署默认**不启动**（compose profile 隔离）。首次启用：

```powershell
docker compose --profile simulator up -d simulator
```

启用后「高级配置 → 虚拟设备」开关即可随时启停（对已创建容器做 docker start/stop）。日志在 `logs/ha_simulator.log`。

## 配置文件位置

| 配置 | 路径 | 说明 |
|------|------|------|
| HA 配置 | `ha_config/` | 挂载到容器 `/config`，含 `configuration.yaml`、`automations.yaml` 等 |
| MQTT 配置 | `mosquitto/config/mosquitto.conf` | 监听 1884 端口，关匿名（凭证 `aether`/`aether`） |

### Mosquitto 配置

```conf
listener 1884
allow_anonymous false
password_file /mosquitto/config/passwd
log_type all
connection_messages true
```

> Mosquitto 关了匿名访问，`mosquitto/init.sh` 首次启动自动生成 `passwd`（用户 `aether`）。`passwd` 被 .gitignore 排除，新 clone 的仓库首次 `docker compose up` 由 init.sh 自动生成。

## 常见问题

**Q：`docker compose up` 报错说找不到 `homeassistant/home-assistant:stable` 镜像？**
A：官方镜像会自动拉取；报错一般是网络问题（国内拉 Docker Hub 慢/超时）。重试、换镜像加速源，或确认镜像名正确。

**Q：HA 打开了但没设备？**
A：模拟器默认关闭。启用：`docker compose --profile simulator up -d simulator`（或本地手动跑 `ha_simulator.py`）。检查 `logs/ha_simulator.log`。

**Q：8123 端口被占？**
A：可能是上次 HA 没关干净。`docker compose down` 后再 `up`，或关掉占用 8123 的其他程序。

**Q：MQTT 连不上？**
A：确认 1884 端口没被占（不是默认的 1883，Aether 用 1884 避免冲突）。`docker logs mosquitto` 看容器日志。
