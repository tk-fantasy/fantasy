<div align="center">

# 🏠 Aether

**A smart-home AI butler powered by LLMs**

Control devices in natural language · Perceive the environment through cameras · Run scheduled automations · Retrieve knowledge via a semantic graph

English | [中文](README.md)

[![Docker](https://img.shields.io/badge/Docker-one_click_deploy-2496ED?logo=docker&logoColor=white)](#-quick-start-docker-recommended)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](requirements.txt)
[![Vue](https://img.shields.io/badge/Vue_3-frontend-4FC08D?logo=vue.js&logoColor=white)](frontend)
[![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)](app)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

<br/>
<img src="docs/images/Home.webp" alt="Aether" width="720"/>
<br/>

</div>

---

## 📸 Feature tour

### 🏠 Chat home — your AI butler

Aether's core interface. Type natural language to control your home — "turn off the living-room lights", "set the AC to 26°C" — and the AI understands your intent and executes it. The left panel supports multi-turn conversation, and slash commands let you jump straight to device management, schedules, and more.

<br/>
<img src="docs/images/Home.webp" alt="Chat home" width="720"/>
<br/>

### ⚙️ Basic settings — personalize your home

Configure household name, owner's preferred form of address, region, and so on. Aether uses these to tailor its service to your life. You can also switch between dark/light themes here.

<br/>
<img src="docs/images/Settings.webp" alt="Basic settings" width="720"/>
<br/>

### 🔧 Advanced settings — the system-level config center

Manage all low-level configuration in one place: weather API keys, Exa search engine, camera vision params, Home Assistant connection, assistant persona. Every config item is a card — click to open an edit panel. Also supports emoji-index rebuild and doc-vector rebuild.

<br/>
<img src="docs/images/advence.webp" alt="Advanced settings" width="720"/>
<br/>

### 🤖 Model management — plug into any LLM

Visually manage all LLM model configs. Set up **chat**, **vision**, **embed**, and **summary** models separately, and switch between providers (OpenAI, Claude, DeepSeek, etc.). Each account can configure its own API keys independently without interfering with others.

<br/>
<img src="docs/images/models.webp" alt="Model management" width="720"/>
<br/>

### 💡 Device control — a clear device panel

Shows all smart devices connected to Home Assistant and their real-time state. Light brightness and color temperature, AC mode and target temp, curtain position — all visualized. You can also control devices straight from the panel without opening chat.

<br/>
<img src="docs/images/device.webp" alt="Device control" width="720"/>
<br/>

### ⏰ Schedules — create timers in natural language

Describe what you want scheduled in natural language and Aether parses it into a cron expression. "Open the curtains at 7 every morning", "Turn off all lights at 10 PM on weekdays" — one sentence and the schedule is created, with an auto-generated task name.

<br/>
<img src="docs/images/schedule.webp" alt="Schedules" width="720"/>
<br/>

### 🔄 Automation rules — condition-driven, smart decisions

Create condition-based automation rules: turn on the AC when it's over 30°C, turn on the entry light when motion is detected. Multi-condition combos and multi-step actions make the home truly "alive". Context-only rule evaluation resolves the LLM key by the rule's creator.

<br/>
<img src="docs/images/auto_task.webp" alt="Automation rules" width="720"/>
<br/>

### 👁️ Vision — let the AI see your home

Connect an RTSP or USB camera and the AI analyzes the scene in real time. Supports motion-triggered visual reasoning and proactively notifies you when something unusual happens. Ask the camera in chat "what do you see".

<br/>
<img src="docs/images/vision.webp" alt="Vision" width="720"/>
<br/>

### 🎯 Focus items — tell the AI what you care about

Customize which objects and areas the vision system watches. You can pin specific regions (doorway, window) or specific objects (person, pet, package). Notifications fire only when a focus item appears, avoiding pointless frequent alerts.

<br/>
<img src="docs/images/focus.webp" alt="Focus items" width="720"/>
<br/>

### 🧠 Semantic graph — visualize your knowledge

A semantic knowledge graph built on RAG. Documents and device info are vectorized, then entity extraction and relation analysis produce an interactive 3D graph. faiss vector retrieval pinpoints relevant info fast, with one-click rebuild.

<br/>
<img src="docs/images/sg_generate.webp" alt="Semantic graph" width="720"/>
<br/>

### 💬 Conversation — silky streaming output

AI replies stream token-by-token, like a person typing. Markdown rendering with code-block highlighting keeps the chat both smart and pretty. Device-control results show as structured cards so outcomes are clear at a glance.

<br/>
<img src="docs/images/generate_show.webp" alt="Conversation" width="720"/>
<br/>

---

## 🛠️ What it does

- **🧠 Conversational device control** — Talks to Home Assistant. Say "turn off the living-room lights and set the AC to 26°C" and it does it. A read-only `verify_action` step runs before any state change so the model can't fire invalid service calls.
- **👁️ Camera vision** — RTSP / USB input, motion-triggered visual reasoning, configurable focus items.
- **⏰ Schedules & automation rules** — Natural-language cron generation, auto task naming, a rule engine that chains devices on conditions.
- **📊 Semantic knowledge graph (RAG)** — Docs vectorized + faiss retrieval + entity co-occurrence graphing, 3D visualization, auto-detect on embed-model change + one-click rebuild.
- **🔌 MCP tool ecosystem** — Built-in weather / web search / device-control tools, plus support for external MCP servers.
- **🔐 JWT auth + independent config** — Sessions use JWT, LLM keys are managed independently with isolated sessions, one-click clear of conversation history.

## Architecture & ports

| Service | Port | Purpose |
|---------|------|---------|
| Aether app | `8010` | FastAPI main service (REST + WebSocket + SPA hosting) |
| Startup progress | `8011` | Cold-start progress endpoint, polled by the loading page before 8010 is up |
| Home Assistant | `8123` | The smart-home brain; Aether drives it via REST API |
| Mosquitto MQTT | `1884` | Virtual-device simulator → HA message channel |
| Vite dev server | `5173` | Frontend dev only; production serves built assets from 8010 |

```
┌─────────────┐   REST    ┌─────────────────┐   MQTT   ┌───────────┐
│  Browser SPA │ ────────► │  Aether (8010)  │ ◄──────► │  HA (8123)│
│  Vue 3 + Vite│           │  FastAPI + LLM  │          └───────────┘
└─────────────┘           └────────┬────────┘                ▲
                                   │ SQLite / faiss          │ MQTT
                                   ▼                         │
                            ┌──────────────┐         ┌──────────────┐
                            │ app/data,logs│         │ Mosquitto    │
                            └──────────────┘         │  (1884)      │
                                                     └──────────────┘
```

## Quick start (Docker, recommended)

A single `docker compose up` brings up all three services. Prerequisites:

```bash
# 1. Configure secrets
cp .env.example .env
#   edit .env and fill in your LLM / STT API keys

# 2. Copy the config template
cp config.example.json config.json
#   leave ha.token empty for now — you'll paste it in the setup wizard
```

Start:

```bash
docker compose up -d --build      # add --build on first run or after code changes
docker compose ps                 # all four containers Up (aether, aether-ha, mosquitto, aether-simulator)
```

Open `http://localhost:8010`. On first run you'll go through a setup wizard:
1. Household info (name, owner's preferred form of address, region)
2. LLM model config (chat / vision / embed / summary — chat is required at minimum)
3. Home Assistant connection — first complete account registration at `http://localhost:8123` and create a long-lived access token

> **HA first-time setup** (required for new users):
> 1. Open `http://localhost:8123` → create an admin account (onboarding flow: name / password / location).
> 2. **Configure the MQTT integration** so the device simulator can report to HA:
>    ```bash
>    docker exec aether-ha python /config/add_mqtt_config.py
>    docker compose restart homeassistant
>    ```
>    The script creates an MQTT integration pointing at the mosquitto container (broker=`mqtt`, port=`1884`, user=`aether`). You can also add it manually via HA UI: Settings → Devices & Services → Add Integration → MQTT.
> 3. After logging in, click the avatar at the bottom-left → Long-Lived Access Tokens → create a token → paste the JWT into step 3 of the wizard.
>
> The repo **does not ship** HA runtime state files (onboarding / auth / entity_registry etc.) — every clone starts with a clean HA that must go through the onboarding above. `ha_config/.storage/core.config` keeps default location/timezone, and `ha_config/mqtt/*.yaml` are simulator device declarations auto-loaded by HA on startup.

> **Built-in demo devices**: the repo ships 11 virtual devices (3 lights / 1 AC / 1 curtain / 1 fan / 1 humidifier / 2 sensors / 2 plugs) declared in `ha_config/mqtt/*.yaml`, with state published by the `aether-simulator` container via MQTT. The goal is to let users without real smart-home hardware experience the full AI control flow (chat to turn on lights, adjust AC, etc.) out of the box.
>
> **How to handle demo devices when connecting your real HA**:
> - Option 1 (recommended): disable the corresponding entities in HA UI (Settings → Devices & Services → MQTT), or delete `ha_config/mqtt/*.yaml` and restart the HA container.
> - Option 2: comment out the `simulator` service and `aether`'s `depends_on: simulator` in `docker-compose.yml`, then `docker compose up -d`.
> - Option 3: keep the demo devices — Aether will see both real and demo devices; refer to them by name in chat.

- App logs: `docker compose logs -f aether`
- Stop: `docker compose down` (data persists in Docker volumes and the mounted `logs/` directory)

> **Camera**: defaults to RTSP network stream (`vision.rtsp_url`); the container needs no special device permissions as long as the camera IP is reachable from the container network. For a local USB camera, add `devices: ["/dev/video0:/dev/video0"]` to the `aether` service (Linux hosts only).

## Local development (without Docker)

Useful when you want hot-reload while editing. Backend on uvicorn, frontend on Vite:

```bash
# Backend deps
python -m pip install -r requirements.txt

# Frontend deps
cd frontend && npm install

# Frontend dev server (port 5173, proxies /api and /ws to 8010)
npm run dev

# Backend (project root, separate terminal)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8010

# Build frontend and sync to the backend static dir (before production deploy)
npm run build
```

> For production use Docker: `docker compose up -d`, then open `http://localhost:8010`. Stop with `docker compose down`.

## Remote access from outside the home (Tailscale)

Aether is intended for the home LAN by default. To use it from your phone on the road, **don't forward ports to the public internet** — use Tailscale (a WireGuard-based point-to-point VPN) instead. Only devices on your own Tailscale network can reach it.

The gist:

1. Install Tailscale on both your PC and phone, sign in with the same account, each device gets a `100.x.x.x` IP.
2. The backend already binds `0.0.0.0:8010` (listens on all interfaces) — no command change needed.
3. Add a Windows firewall rule that **only** allows the Tailscale range `100.64.0.0/10` to reach port 8010:

```powershell
New-NetFirewallRule -DisplayName "Aether Backend 8010 (Tailscale only)" `
  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8010 `
  -RemoteAddress 100.64.0.0/10 -Profile Any
```

4. On your phone, browse to `http://<PC's Tailscale IP>:8010/` (run `tailscale ip -4` on the PC to find it).

> **Use port 8010, not 5173**: 5173 is the Vite dev server and only listens on `127.0.0.1`, so external devices can't reach it. 8010 hosts both the frontend page and the API — it's the port for both daily use and remote access.

Full instructions (firewall profile gotchas, troubleshooting) in [`docs/01-安装部署/Tailscale远程访问与防火墙配置.md`](docs/01-安装部署/Tailscale远程访问与防火墙配置.md). The backend CORS already allows the Tailscale `100.64.0.0/10` range (see `allow_origin_regex` in `app/main.py`).

## Configuration

| File | Purpose | In VCS? |
|------|---------|:---:|
| `.env` | API keys (LLM / STT / HA token, etc.), injected via env vars | ✗ |
| `config.json` | App runtime config (LLM key mapping, HA connection, vision, weather, …) | ✗ |
| `.env.example` / `config.example.json` | Templates for the above | ✓ |

Environment variables take precedence and override `config.json`:

| Env var | Overrides | Purpose |
|---------|-----------|---------|
| `HA_URL` | `ha.url` | Points to `http://homeassistant:8123` inside the compose network |
| `HA_TOKEN` | `ha.token` | HA long-lived access token |
| `LLM_ENABLED` / `LLM_BASE_URL` / `LLM_MODEL` | `llm.*` | Global LLM toggle and model |
| `LOG_LEVEL` | `logging.level` | Log level |
| `STARTUP_PROGRESS_HOST` | — | Bind address for the startup-progress port (set to `0.0.0.0` in container) |

> LLM keys are best managed via the "API Keys" card on the Advanced page — it writes to `.env` and persists metadata to the database automatically.

## Project layout

```
app/
├── main.py              # FastAPI entry: lifecycle, middleware, routing, SPA hosting
├── bootstrap.py         # Service initialization (constructs all service instances)
├── container.py         # DI container (AppContainer — no `from main import` globals)
├── core/                # Config, database, auth, rate-limit, exceptions, tracing
├── clients/             # HA / LLM (chat / vision / embed) HTTP clients
├── services/            # Business services (rules, scheduler, vision, weather, sessions, RAG, semantic graph…)
├── agents/              # LangGraph agent, Dispatcher, Validator
├── mcp/                 # MCP tools (local tools, external servers, tool executor)
├── routes/              # REST/WS routes (split by feature)
├── sg/                  # Semantic-graph pipeline (entity extraction, vectorization, relation analysis, graph build)
├── schema/              # Request/response schemas
└── data/                # SQLite DB, JWT secret, emoji vector index (generated at runtime)
frontend/                # Vue 3 + Vite frontend
ha_config/               # Home Assistant config (mounted into HA container /config; only templates tracked, runtime state generated by HA)
mosquitto/               # Mosquitto MQTT config
tests/                   # Backend pytest (740+ tests)
frontend/tests/          # Frontend vitest
docs/                    # User-facing + technical docs (organized by feature)
```

## Tests

```bash
# Backend
pytest                      # all tests
pytest -m "not slow"        # skip slow tests that need real API calls
pytest tests/test_dispatcher.py   # a single module

# Frontend
cd frontend && npm test
```

CI runs `pytest -m "not slow"` on every push and pull request via GitHub Actions.

## Documentation

Full docs live under `docs/`, organized by feature (Chinese, English translation in progress):

- `docs/01-安装部署/` — environment prep, Docker deploy, HA connection, LLM keys, weather API, Tailscale remote
- `docs/02-AI聊天/` — chat basics, persona customization, model roles, session management, slash commands
- `docs/03-设备控制/` — natural-language control, device widgets, device panel
- `docs/04-自动化规则/` — scheduled tasks, automation rules, rule maintenance, vision triggers
- `docs/05-摄像头视觉/` — camera input, focus-item config, motion detection
- `docs/06-集成扩展/` — Exa search, MQTT integration, external MCP
- `docs/07-个性化/` — emoji customization, household info & theme
- `docs/08-运维排查/` — API auth, log inspection, health checks
- `docs/tech/` — architecture overview, API/MCP reference, scheduler/automation engine, vision subsystem, config reference

## UI navigation

Four sidebar entries; other features are reachable via slash commands:

| Entry | Description |
|-------|-------------|
| **Butler** | Main chat. Type `/` to see all slash commands (devices, schedules, models, one-click jumps). |
| **Settings** | Household info, region, dark mode. |
| **Advanced** | System-level config page: weather API, Exa search, vision params, HA connection, assistant persona, API Keys (click a card to edit in a modal), plus emoji-index rebuild and doc-vector rebuild. |
| **Monitor** | System monitoring page (also reachable via `/monitor` in chat). |

---

## 🤝 Contributing

Issues and PRs are welcome. If you'd like to show up in the contributors list, just open a PR — GitHub identifies contributors automatically by commit email.

---

<div align="center">

**Built with ❤️ by [Aether Demo](https://github.com/Aether-Demo)**

</div>

## License

MIT — see [LICENSE](LICENSE).
