# Aether

> 一个接入了大模型（LLM）的智能家居 AI 管家：用自然语言控制设备、视觉感知环境、定时自动化、语义知识图谱检索，前端是 Vue 3 单页应用，后端是 FastAPI。

## 它能做什么

- **AI 对话控制设备** —— 对接 Home Assistant，自然语言开灯 / 调空调 / 拉窗帘，调用前先��� `verify_action` 只读校验
- **摄像头视觉感知** —— RTSP / USB 接入，运动检测触发视觉推理，关注项可配置
- **定时任务与自动化规则** —— 自然语言生成 cron 触发时间，任务名自动生成，规则引擎按条件联动设备
- **语义知识图谱（RAG）** —— 文档向量化 + faiss 检索 + 实体共现构图，3D 可视化，embed 模型变更后自动检测 + 一键重建
- **MCP 工具生态** —— 内置天气 / 网页搜索 / 设备控制工具，可接外部 MCP Server
- **多用户 + JWT 鉴权** —— 每用户独立 LLM Key 管理，会话隔离，支持一键清空历史会话

## 架构与端口

| 服务 | 端口 | 说明 |
|------|------|------|
| Aether 应用 | `8010` | FastAPI 主服务（REST + WebSocket + 前端 SPA 托管） |
| 启动进度 | `8011` | 冷启动进度上报，供加载页轮询（主端口就绪前可用） |
| Home Assistant | `8123` | 智能家居大脑，Aether 通过其 REST API 控制设备 |
| Mosquitto MQTT | `1884` | 虚拟设备模拟器 → HA 的消息通道 |
| Vite 开发服务器 | `5173` | 仅前端本地开发用，生产环境由 8010 直接托管构建产物 |

```
┌─────────────┐   REST    ┌─────────────────┐   MQTT   ┌───────────┐
│  浏览器 SPA  │ ────────► │  Aether (8010)  │ ◄──────► │  HA (8123)│
│  Vue 3 + Vite│           │  FastAPI+LLM    │          └───────────┘
└─────────────┘           └────────┬────────┘                ▲
                                   │ SQLite / faiss          │ MQTT
                                   ▼                         │
                            ┌──────────────┐         ┌──────────────┐
                            │ app/data,logs│         │ Mosquitto    │
                            └──────────────┘         │  (1884)      │
                                                     └──────────────┘
```

## 快速开始（Docker，推荐）

一条 `docker compose up` 起全部三个服务。前置准备：

```bash
# 1. 配置密钥
cp .env.example .env
#   编辑 .env，填入 LLM / STT 的 API Key

# 2. 复制配置模板
cp config.example.json config.json
#   ha.token 留空即可，稍后在引导向导里填
```

启动：

```bash
docker compose up -d --build      # 首次或代码更新后加 --build
docker compose ps                 # 四个容器都 Up 即可（aether, aether-ha, mosquitto, aether-simulator）
```

打开 `http://localhost:8010` 进入应用，首次需走引导向导：
1. 家庭信息（名称、主人称呼、地区）
2. LLM 模型配置（对话/视觉/嵌入/摘要，至少配对话模型）
3. Home Assistant 连接 —— 需要先到 `http://localhost:8123` 完成账号注册并创建长期访问令牌

> **HA 首次初始化**：打开 `http://localhost:8123` → 创建管理员账号 → 登录后左下角头像 → 长期访问令牌 → 创建令牌 → 复制 JWT 粘贴到引导向导第 3 步

- 应用日志：`docker compose logs -f aether`
- 停止：`docker compose down`（数据保留在 Docker volume 和 `logs/` 挂载目录）

> **摄像头**：默认走 RTSP 网络流（`vision.rtsp_url`），容器无需特殊设备权限，只要摄像头 IP 在容器网络可达。若改用本地 USB 摄像头，需在 `aether` 服务加 `devices: ["/dev/video0:/dev/video0"]`（仅 Linux 主机）。

## 本地开发（不走 Docker）

适合改代码时热重载。后端用 uvicorn，前端用 Vite 开发服务器：

```bash
# 后端依赖
python -m pip install -r requirements.txt

# 前端依赖
cd frontend && npm install

# 前端开发服务器（5173，代理 /api /ws 到 8010）
npm run dev

# 后端（项目根目录，另开终端）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8010

# 前端构建并同步到后端静态目录（生产部署前）
npm run build
```

> 生产部署用 Docker：`docker compose up -d`，然后浏览器访问 `http://localhost:8010`。停止用 `docker compose down`。

## 从外面远程访问（Tailscale）

Aether 默认只在家里局域网用。想在外面用手机访问，**不要做端口转发暴露公网**，用 Tailscale（基于 WireGuard 的点对点 VPN）更安全——只有你自己 Tailscale 网络里的设备能连进来。

核心做法：

1. 电脑和手机都装 Tailscale，同账号登录，各分到一个 `100.x.x.x` 内网 IP
2. 后端已绑 `0.0.0.0:8010`（监听所有网卡），无需改启动命令
3. 加一条 Windows 防火墙规则，**只放行 Tailscale 网段 `100.64.0.0/10`** 访问 8010：

```powershell
New-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8010 `
  -RemoteAddress 100.64.0.0/10 -Profile Any
```

4. 手机浏览器访问 `http://<电脑的Tailscale IP>:8010/`（用 `tailscale ip -4` 查电脑 IP）

> **访问 8010，不是 5173**：5173 是 Vite 开发服务器，只监听 `127.0.0.1`，外部设备连不上。8010 同时托管前端页面和 API，是日常使用和远程访问都该用的端口。

详细步骤、防火墙 profile 踩坑、故障排查见 [`docs/01-安装部署/Tailscale远程访问与防火墙配置.md`](docs/01-安装部署/Tailscale远程访问与防火墙配置.md)。后端 CORS 已预放行 Tailscale `100.64.0.0/10` 网段（`app/main.py` 的 `allow_origin_regex`）。

## 配置文件

| 文件 | 作用 | 是否进版本库 |
|------|------|:---:|
| `.env` | API 密钥（LLM / STT / HA 令牌等），通过环境变量注入 | ✗ |
| `config.json` | 应用运行配置（LLM keys 映射、HA 连接、视觉、天气等） | ✗ |
| `.env.example` / `config.example.json` | 上述两者的模板 | ✓ |

环境变量优先级最高，可覆盖 `config.json`：

| 环境变量 | 覆盖的配置 | 用途 |
|----------|-----------|------|
| `HA_URL` | `ha.url` | 容器内指向 `http://homeassistant:8123` |
| `HA_TOKEN` | `ha.token` | HA 长期访问令牌 |
| `LLM_ENABLED` / `LLM_BASE_URL` / `LLM_MODEL` | `llm.*` | LLM 全局开关与模型 |
| `LOG_LEVEL` | `logging.level` | 日志级别 |
| `STARTUP_PROGRESS_HOST` | — | 启动进度端口绑定地址（容器内设 `0.0.0.0`） |

> LLM 密钥推荐用「高级」页面的 API Keys 卡片管理，会自动写入 `.env` 并持久化到数据库。

## 项目结构

```
app/
├── main.py              # FastAPI 入口：生命周期、中间件、路由注册、SPA 托管
├── bootstrap.py         # 服务初始化（构造所有服务实例）
├── container.py         # DI 容器（AppContainer，消除 from main import 全局）
├── core/                # 配置、数据库、鉴权、限流、异常、链路追踪
├── clients/             # HA / LLM（chat/vision/embed）HTTP 客户端
├── services/            # 业务服务（规则、调度、视觉、天气、会话、RAG、语义图…）
├── agents/              # LangGraph Agent、Dispatcher、Validator
├── mcp/                 # MCP 工具（本地工具、外部 Server、工具执行器）
├── routes/              # REST/WS 路由（按功能分模块）
├── sg/                  # 语义图 pipeline（实体抽取、向量化、关系分析、构图）
├── schema/              # 请求/响应 Schema
└── data/                # SQLite 库、JWT 密钥、emoji 向量索引（运行时生成）
frontend/                # Vue 3 + Vite 前端
ha_config/               # Home Assistant 配置（挂载到 HA 容器 /config）
mosquitto/               # Mosquitto MQTT 配置
tests/                   # 后端 pytest（66+ 测试）
frontend/tests/          # 前端 vitest
docs/                    # 用户层 + 技术层文档（按功能分类）
```

## 测试

```bash
# 后端
pytest                      # 全部
pytest -m "not slow"        # 跳过需要真实 API 调用的慢测试
pytest tests/test_dispatcher.py   # 单个模块

# 前端
cd frontend && npm test
```

## 文档

详细文档在 `docs/` 下，按功能分类：

- `docs/01-安装部署/` —— 环境准备、Docker 部署、HA 连接、LLM 密钥、天气 API、Tailscale 远程
- `docs/02-AI聊天/` —— 聊天入门、人格自定义、模型角色、会话管理、斜杠命令
- `docs/03-设备控制/` —— 自然语言控制、设备控件、设备面板
- `docs/04-自动化规则/` —— 定时任务、自动化规则、规则维护、视觉触发
- `docs/05-摄像头视觉/` —— 摄像头接入、关注项配置、运动检测
- `docs/06-集成扩展/` —— Exa 搜索、MQTT 接入、外部 MCP
- `docs/07-个性化/` —— Emoji 自定义、家庭信息与主题
- `docs/08-运维排查/` —— API 鉴权、日志���查、健康检查
- `docs/tech/` —— 架构概述、API/MCP 参考、调度/自动化引擎、视觉子系统、配置参考

## 界面导航

侧边栏四个入口，其余功能通过斜杠命令到达：

| 入口 | 说明 |
|------|------|
| **管家** | 聊天主界面，输入 `/` 查看全部斜杠命令（设备、定时、模型等一键跳转） |
| **设置** | 家庭信息、地区、深色模式 |
| **高级** | 系统级配置页面：天气 API、Exa 搜索、视觉参数、HA 连接、助手角色、API Keys（点击卡片弹出 modal 编辑），以及 Emoji 索引重建、文档向量重建 |
| **监控** | 系统监控页面，也可在聊天输入 `/monitor` 跳转 |

> `/ha`、`/unique`、`/keys` 三个路由保留向后兼容，其配置内容已并入「高级」页面。
