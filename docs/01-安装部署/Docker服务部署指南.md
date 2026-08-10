# Docker 服务部署指南

这篇讲怎么用 Docker 把 Aether 跑起来。

## 四个服务

Aether 编排四个容器，都定义在 `docker-compose.yml` 里：

1. **aether**——后端主服务（API + WebSocket + 前端页面），Dockerfile 构建
2. **Home Assistant（智能家居大脑）**——管理所有智能设备，Aether 通过它的 API 控制全屋
3. **Mosquitto MQTT（消息中转）**——一个"消息邮局"，虚拟设备把状态发到这里，HA 从这里接收
4. **aether-simulator（虚拟设备模拟器）**——通过 MQTT 往 HA 上报 11 个演示设备（灯/空调/窗帘等），接真实设备后可注释掉

> 老版本里还有第五个 SearXNG 搜索引擎，现在已经换成云端的 **Exa MCP** 搜索（不占本地端口、不用本地容器），所以 Docker 只剩四个容器了。

## 开始之前

确认三件事：

- 装了 **Docker Desktop**（Windows 版）
- Docker 在运行（任务栏图标是绿色）
- 终端能跑 `docker --version`

## 关于 HA 镜像

Aether 用官方镜像 `homeassistant/home-assistant:stable`，配置通过 `./ha_config` 挂载进去。只用了 `default_config` + 内置 MQTT 集成，没有自定义组件，`docker compose up -d` 自动拉取，无需手动构建。

## 启动服务

在项目根目录运行：

```powershell
docker compose up -d --build
```

第一次启动会拉取 mosquitto、HA、构建 Aether 镜像（前端 npm ci + vite build + 后端 pip install），约 5–10 分钟。之后启动很快。

### 看看是不是跑起来了

```powershell
docker compose ps
```

四个容器状态都是 `Up` 就搞定了：

| 容器名 | 镜像 | 端口 |
|--------|------|------|
| `aether` | 本地构建 | 8010→8010, 8011→8011 |
| `aether-ha` | `homeassistant/home-assistant:stable` | 8123→8123 |
| `mosquitto` | `eclipse-mosquitto:2` | 1884→1884 |
| `aether-simulator` | `python:3.11-slim` | — |

> 小提示：日常启动只需 `docker compose up -d`，会自动起全部四个服务。

## 停止服务

```powershell
# 停掉容器，数据还在
docker compose down

# 停掉容器并清掉数据卷（恢复出厂设置）
docker compose down -v
```

> 用 `-v` 会删容器内数据卷，但你改的本地配置文件（`ha_config/`、`mosquitto/config/`）不会丢。

## 打开智能家居管理界面

浏览器访问：

```
http://localhost:8123
```

### 第一次进 HA 要做的事

- 如果是新启动的数据卷，HA 会让你创建管理员账号——随便填，这是 HA 自己的账号，和 Aether 的登录账号没关系
- 进去后应该能看到一些虚拟设备（灯、空调等），它们是 `ha_simulator.py` 模拟器通过 MQTT 报上来的
- 确认设备列表不空，说明 MQTT 链路通了

## 虚拟设备模拟器

Aether 自带一个虚拟设备模拟器 `ha_config/ha_simulator.py`，它通过 MQTT 往 HA 报告虚拟设备状态（灯、空调、窗帘、传感器等），让没有真实硬件也能演示。

```powershell
conda run -n yolo python ha_config\ha_simulator.py
```

`docker compose up -d` 会自动启动它，日志在 `logs/ha_simulator.log`。

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
A：模拟器没启动。检查 `logs/ha_simulator.log`，或手动跑 `ha_simulator.py`。

**Q：8123 端口被占？**
A：可能是上次 HA 没关干净。`docker compose down` 后再 `up`，或关掉占用 8123 的其他程序。

**Q：MQTT 连不上？**
A：确认 1884 端口没被占（不是默认的 1883，Aether 用 1884 避免冲突）。`docker logs mosquitto` 看容器日志。
