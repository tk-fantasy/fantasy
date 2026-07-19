# Aether

[English](README.en.md) | [中文](README.md)

> A smart-home AI butler powered by LLMs: control devices in natural language, perceive the environment through cameras, run scheduled automations, and retrieve knowledge via a semantic graph. Backend in FastAPI, frontend in Vue 3.

## What it does

- **Conversational device control** — Talks to Home Assistant. Say "turn off the living-room lights and set the AC to 26°C" and it does it. A read-only `verify_action` step runs before any state change so the model can't fire invalid service calls.
- **Camera vision** — RTSP or USB input. Motion detection triggers on-demand visual reasoning (configurable focus items, e.g. "is the stove on?").
- **Schedules & automation rules** — Natural-language cron generation, auto task naming, a rule engine that chains devices on conditions (motion → camera → notification).
- **Semantic knowledge graph (RAG)** — Docs are vectorized with faiss, entities are co-occurrence linked into a graph, visualized in 3D. Auto-detects embed-model changes and offers one-click rebuild.
- **MCP tool ecosystem** — Built-in weather / web search / device-control tools, plus support for external MCP servers.
- **Multi-user + JWT auth** — Per-user LLM key management, isolated sessions, per-user model routing with global fallback.

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

> **HA first-time setup**: open `http://localhost:8123` → create an admin account → click the avatar at the bottom-left → Long-Lived Access Tokens → create a token → paste the JWT into step 3 of the wizard.

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
ha_config/               # Home Assistant config (mounted into the HA container as /config)
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

## License

MIT — see [LICENSE](LICENSE).
