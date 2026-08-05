"""Aether 应用入口。"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 启动进度上报：先于任何重依赖导入，起一个轻量进度端口（8011），
# 让加载页在冷启动期间能拿到真实的加载阶段（主端口 8010 此时尚未监听）。
from .startup_progress import startup_progress as _startup_progress
_startup_progress.start()
_startup_progress.set("正在加载后端依赖...")

import faiss
import numpy as np
import openai
import httpx as _httpx

from fastapi import FastAPI, WebSocket, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse

from .agents.automation_agent import AutomationAgent
from .agents.dispatcher import Dispatcher
from .bootstrap import initialize_services
from .container import AppContainer, get_container, init_container
from .core import ApiResponse, CameraStateModel, Database, HealthData
from .core.config import get_config, update_config_section
from .core.rate_limit import global_limiter
from .core.tracing import RequestIdFilter, new_request_id, set_request_id
from .mcp.web_tools import close_http_client as close_web_http_client
from .services.health_check import HealthChecker
from .services.metrics_service import MetricsService
from .services.scheduler_service import SchedulerService
from .tools import ToolDeps, connect_external_mcp_servers, register_all_tools
from .utils.async_utils import create_task_manager
from .utils.handlers import register_exception_handlers

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOG_DIR = BASE_DIR.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

# 有界线程池：WebSocket 流式 LLM 调用、RAG 构建等
_stream_executor = _ThreadPoolExecutor(max_workers=8, thread_name_prefix="stream")

# Windows 控制台 UTF-8 输出（测试环境跳过：替换 sys.stdout 会破坏 pytest capture）
if sys.platform == "win32" and not os.getenv("PYTEST_CURRENT_TEST", "") and "pytest" not in sys.modules:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | [%(request_id)s] %(message)s",
    handlers=[
        # 控制台日志走 stderr（日志不应污染 stdout；同时避免 pytest capture 冲突）
        logging.StreamHandler(stream=sys.stderr),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
    force=True,
)

# 给所有 handler 添加 request_id 过滤器
_request_id_filter = RequestIdFilter()
for _handler in logging.root.handlers:
    _handler.addFilter(_request_id_filter)

# 使用统一的 TaskManager 管理后台任务
_background_task_mgr = create_task_manager()

# ============ 初始化所有服务 ============
_startup_progress.set("正在初始化服务...")
_services = initialize_services()

# 从 services dict 提取全局引用（容器已持有全部服务，此处仅留 main.py 内部直接使用的引用）
vision_client = _services["vision_client"]
vision_service = _services["vision_service"]
camera_stream = _services["camera_stream"]
llm_chat_client = _services["llm_chat_client"]
embed_client = _services["embed_client"]
emoji_service = _services["emoji_service"]
session_store = _services["session_store"]
mcp_client_manager = _services["mcp_client_manager"]
tool_executor = _services["tool_executor"]
summarization_service = _services["summarization_service"]
rule_service = _services["rule_service"]
rule_registry_service = _services["rule_registry_service"]
automation_service = _services["automation_service"]
langgraph_agent = _services["langgraph_agent"]
ha_client = _services["ha_client"]
ha_service = _services["ha_service"]
_automation_agent_ref = _services["automation_agent_ref"]
_ha_catalog_cache_ref = _services["ha_catalog_cache_ref"]
_ha_client_ref = _services["ha_client_ref"]
discovery_service = _services["discovery_service"]
_ha_controls_cache_ref = [""]

# Metrics 服务（轻量内存计数器）
metrics_service = MetricsService()

# 健康检查器（跟踪外部服务可用性）
health_checker = HealthChecker()

# 初始化 DI 容器
_container = init_container(_services, metrics_service)
# 补充 main.py 特有的可变引用
_container.ha_controls_cache_ref = _ha_controls_cache_ref
# RAG 服务（索引在 lifespan 启动阶段后台构建）
from .services.rag_service import RagService
rag_service = RagService(base_dir=BASE_DIR, embed_client=embed_client)
_container.rag_service = rag_service
# embed 模型变更时自动重建 RAG 索引（钩子在 embed_client.reload 之后执行）
_services["llm_settings_service"].register_reload_hook(rag_service.maybe_rebuild_if_model_changed)


def _get_ha_device_catalog() -> str:
    return _ha_catalog_cache_ref[0]


def _get_ha_device_controls() -> str:
    return _ha_controls_cache_ref[0]


# ============ Agent 重建（MCP 工具变更后调用） ============

_rebuild_lock = asyncio.Lock()


async def _rebuild_agent() -> None:
    """重新转换工具并重建 LangGraph Agent，更新 dispatcher 引用。

    调用方必须持有 _rebuild_lock。
    """
    from .mcp.langchain_tools import convert_all_tools
    from .agents.langgraph_agent import build_chat_agent

    langchain_tools = convert_all_tools(mcp_client_manager)
    new_agent, new_clients = build_chat_agent(tools=langchain_tools)
    # 旧客户端的回收交给 dispatcher.set_agent（它内部 close_all_agent_clients）
    global langgraph_agent
    langgraph_agent = new_agent
    _services["langgraph_agent"] = new_agent
    _services["langchain_tools"] = langchain_tools
    if dispatcher is not None:
        await dispatcher.set_agent(new_agent, tools=langchain_tools, clients=new_clients)
    logger.info("Agent rebuilt with %d tools", len(langchain_tools))


# ============ 公共工具函数 ============
# RAG 搜索与 LLM 客户端构建已收敛到 RagService（app/services/rag_service.py），
# 路由通过 container.rag_service 访问。原 _rag_search / _build_rag_llm_client /
# _RAG_SYSTEM_PROMPT_TEMPLATE 模块级封装已移除。


async def _ws_verify_token(websocket: WebSocket) -> str | None:
    """验证 WebSocket 连接的认证，失败则关闭并返回 None。

    支持三种方式：query param > cookie > APP_TOKEN
    """
    # 尝试 JWT 验证（query param → cookie → Authorization header）
    from .core.auth import ACCESS_COOKIE, verify_token
    token = websocket.query_params.get("token") or websocket.cookies.get(ACCESS_COOKIE)
    if not token:
        auth_header = websocket.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if token:
        try:
            payload = verify_token(token)
            return payload.get("sub", "")  # 返回 user_id
        except Exception:
            pass  # JWT 验证失败，继续尝试 APP_TOKEN

    # 向后兼容：检查 APP_TOKEN
    if APP_TOKEN:
        provided = websocket.headers.get("X-API-Token") or websocket.query_params.get("app_token")
        if provided == APP_TOKEN:
            return ""  # APP_TOKEN 验证成功，返回空 user_id

    # 都没有通过验证
    await websocket.close(code=1008)
    return None


async def _ws_heartbeat(websocket: WebSocket, interval: int = 30):
    """WebSocket 心跳，定期发送 ping 保活。"""
    while True:
        await asyncio.sleep(interval)
        try:
            await websocket.send_json({"type": "ping"})
        except Exception:
            break


# ============ 后台任务 ============


async def _refresh_ha_catalog() -> None:
    """后台刷新 HA 设备目录缓存。

    catalog 按物理设备分组组织，帮助 AI 以「物理设备」为单位向用户介绍（而不是把
    同一设备的传感器/诊断属性拆成多个设备念出）。

    关键：每行格式 `- entity_id (类型:domain, 状态:xxx) 名称:xxx` 不能改
    （rule_service._parse_ha_catalog 的正则依赖此格式抠 entity_id 做自动化校验），
    但「名称:」部分统一用父设备名（dev_name），不用子实体的 friendly_name——
    否则「小爱音箱Pro 麦克风 静音」这类子实体名会被 LLM 当独立设备念出。
    """
    try:
        from .services.entity_controls import resolve_controls, controls_to_text
        grouped = await ha_service.get_all_devices_grouped()
        devices = await ha_service.get_all_devices()
        raw_svc_defs = await ha_service.get_service_defs(
            ha_client, domains=set(d.get("domain", "") for d in devices)
        )
        # 诊断/属性类 domain：不作为独立设备条目念给用户
        DIAGNOSTIC_DOMAINS = {"sensor", "binary_sensor"}
        lines = []
        controls_lines = []
        # 用户自定义备注（entity_note scope）：按 entity_id 查 dict 注入 controls。
        # 不常驻 DB 连接——每 60 秒刷新周期读一次即可（备注变更最多 60 秒生效）。
        notes_map: dict[str, str] = {}
        try:
            from .core.database import Database
            notes_map = await Database.get().prefs_get_by_scope("entity_note")
        except Exception:  # noqa: BLE001
            logger.warning("Failed to load entity notes for catalog")
        for dev in grouped.get("devices", []):
            dev_name = dev.get("name", "")
            area_name = dev.get("area_name")
            model = dev.get("model")
            ents = dev.get("entities", [])
            controllable = [e for e in ents if e["domain"] not in DIAGNOSTIC_DOMAINS]
            # 纯诊断设备（无可控实体，如网关）：只留标题行让 AI 知道这设备存在，
            # 不列任何 entity 行（避免把 sensor 的 friendly_name 噪声暴露给 LLM）。
            # rule_service 的 _parse_ha_catalog 只用于「构建自动化规则时解析可用设备」，
            # 纯诊断设备本来就不能被 call_service 控制，缺这几行不影响规则生成。
            if not controllable:
                header = f"# {dev_name}"
                if model:
                    header += f" ({model})"
                if area_name:
                    header += f" [{area_name}]"
                lines.append(header)
                continue

            header = f"# {dev_name}"
            if model:
                header += f" ({model})"
            if area_name:
                header += f" [{area_name}]"
            lines.append(header)
            # 可控实体：entity_id 必须保留（call_service 要用），但「名称:」统一用
            # 父设备名，避免子实体 friendly_name 噪声
            for e in controllable:
                eid = e["entity_id"]
                lines.append(
                    f"- {eid} (类型:{e['domain']}, 状态:{e['state']}) 名称:{dev_name}"
                )
            # controls（中文可控项，供 call_service）
            # 按物理设备聚合，标题统一用设备名（dev_name）
            if raw_svc_defs:
                dev_controls_lines = [f"{dev_name}:"]
                for e in controllable:
                    flat = next((d for d in devices if d["entity_id"] == e["entity_id"]), None)
                    if flat:
                        controls = resolve_controls(flat, raw_svc_defs)
                        if controls:
                            dev_controls_lines.append(
                                controls_to_text(flat, controls, indent=1, note=notes_map.get(e["entity_id"]))
                            )
                if len(dev_controls_lines) > 1:
                    controls_lines.append("\n".join(dev_controls_lines))
        catalog = "\n".join(lines) if lines else "(暂无 HA 设备)"
        controls_text = "\n\n".join(controls_lines) if controls_lines else ""
        _ha_catalog_cache_ref[0] = catalog
        _ha_controls_cache_ref[0] = controls_text
    except Exception:  # noqa: BLE001
        logger.warning("HA catalog refresh failed")


async def _ha_catalog_refresh_loop() -> None:
    """后台定时刷新 HA 设备目录缓存(每 60 秒)。"""
    await asyncio.sleep(5)
    while True:
        try:
            await _refresh_ha_catalog()
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.warning("HA catalog refresh loop error")
        await asyncio.sleep(60)


# ============ 生命周期 ============


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期管理。"""

    logger.info(
        "Application startup",
        extra={"llm_enabled": llm_chat_client.enabled, "llm_model": llm_chat_client.model},
    )

    _startup_progress.set("正在初始化数据库...")
    # 初始化数据库
    await Database.init()

    # 异步加载 emoji 索引（不阻塞启动）
    _background_task_mgr.spawn(emoji_service.load_index_async(), name="emoji_index_load")

    _startup_progress.set("正在加载会话与规则...")
    # 从数据库加载持久化数据
    await rule_registry_service.load_from_db()
    await session_store.load_from_db()

    # 加载全局 LLM keys：优先 config.json（全局共享、跨重启持久化）；
    # 仅当 config.json 的 llm_keys 为空时，fallback 到"第一个有 llm_keys 的用户 DB"，
    # 并一次性迁移到 config.json（兼容历史部署：老版本把全局 key 只存用户 DB）。
    try:
        db = Database.get()
        config_keys = get_config("llm_keys", []) or []
        if config_keys:
            logger.info("Loaded %d global LLM keys from config.json", len(config_keys))
        else:
            # config.json 无 key：fallback 到第一个有 llm_keys 的用户 DB，并迁移到 config.json
            all_users = await db.user_list_all()
            migrated = False
            for user in all_users:
                llm_keys_json = await db.user_setting_get(user["id"], "llm_keys")
                if not llm_keys_json:
                    continue
                llm_keys = json.loads(llm_keys_json)
                if not llm_keys:
                    continue
                from .core.config import (
                    update_memory_config,
                    save_global_llm_keys,
                    update_config_section,
                )
                update_memory_config("llm_keys", llm_keys)
                # 同步 providers（老版本把第一个用户的 providers 当全局用，迁移时一并持久化）
                providers_json = await db.user_setting_get(user["id"], "providers")
                migrated_providers: dict = {}
                if providers_json:
                    providers = json.loads(providers_json)
                    if providers:
                        update_memory_config("providers", providers)
                        migrated_providers = providers
                # 一次性迁移到 config.json 持久化（llm_keys 数组 + providers dict）
                try:
                    save_global_llm_keys(llm_keys)
                    if migrated_providers:
                        update_config_section("providers", migrated_providers)
                    migrated = True
                    logger.info(
                        "Migrated %d LLM keys + providers from user '%s' DB to config.json",
                        len(llm_keys), user["username"],
                    )
                except Exception:
                    logger.warning("Failed to persist migrated llm_keys to config.json", exc_info=True)
                break
            if not migrated:
                logger.info("No LLM keys found in config.json or any user DB")
    except Exception as e:
        logger.warning("Failed to load global LLM keys: %s", e)

    # 启动自愈：全局 llm_keys 非空但某些角色 key 无效（空/占位符）时，
    # 从 per-user DB 找第一个有该角色有效明文 api_key 的用户条目恢复。
    # 场景：wizard 把 embed/vision key 同时写进全局 .env（env 引用）和
    # per-user DB（明文）；容器重建后 .env 丢失/占位符 → 全局解析为空，
    # 但 per-user DB 的明文 key 还在。此处一次性恢复，避免 RAG/语义图/emoji 401。
    try:
        from .core.key_healing import heal_global_keys_from_user_db
        healed = await heal_global_keys_from_user_db()
        if healed:
            # 自愈改了内存 CONFIG + env，但 LLM client 实例在自愈前已创建
            # （bootstrap.py 模块级），_api_key 还是占位符。reload 让它们重读。
            try:
                _container.reload_all_clients()
                logger.info("Reloaded LLM clients after healing %d keys", len(healed))
            except Exception as e:
                logger.warning("Failed to reload clients after key healing: %s", e)
    except Exception as e:
        logger.warning("Failed to heal global LLM keys from user DB: %s", e)

    # 一次性迁移 home_info：历史代码把家庭地址按 per-user 存进了 DB（user_settings.home_info），
    # 但 weather_service.get_weather() 读的是全局 config.json 的 home 段，两边不通导致天气组件空白。
    # 此处把已存在 DB 里的 home_info 镜像到 config.json 的 home 段（仅当 config 没有完整 home 时）。
    # 跟上面 LLM keys 的迁移模式一致：DB 仍是兼容性 fallback，config.json 是新真源。
    # 注意：本函数体内 LLM keys 迁移块有局部 `from .core.config import update_config_section`，
    # 会让 Python 把 update_config_section 当成本函数的局部变量；只能走模块全限定名绕开。
    try:
        from . import core as _core  # noqa: WPS433 — 绕开局部 import 作用域陷阱
        db = Database.get()
        home_config = get_config("home", {}) or {}
        home_complete = bool(home_config.get("city") or home_config.get("district"))
        if not home_complete:
            all_users = await db.user_list_all()
            for user in all_users:
                home_info_json = await db.user_setting_get(user["id"], "home_info")
                if not home_info_json:
                    continue
                try:
                    home_data = json.loads(home_info_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not (home_data.get("city") or home_data.get("district")):
                    continue
                _core.config.update_config_section("home", {
                    "home_name": home_data.get("home_name", ""),
                    "owner_name": home_data.get("owner_name", ""),
                    "province": home_data.get("province", ""),
                    "city": home_data.get("city", ""),
                    "district": home_data.get("district", ""),
                })
                logger.info(
                    "Migrated home_info from user '%s' DB to config.json (city=%s)",
                    user["username"], home_data.get("city"),
                )
                break
    except Exception as e:
        logger.warning("Failed to migrate home_info from user DB to config.json: %s", e)

    # 加载视觉关注指令（支持新多条格式 + 旧单条迁移）
    db = Database.get()
    saved_focuses = await db.kv_get("vision_focuses")
    if saved_focuses:
        try:
            focuses = json.loads(saved_focuses)
            vision_service.load_focuses(focuses)
            logger.info("Loaded %d vision_focuses from database", len(focuses))
        except (ValueError, TypeError):
            logger.warning("Failed to parse vision_focuses from database")
    else:
        # 迁移旧 vision_focus
        saved_focus = await db.kv_get("vision_focus")
        if saved_focus:
            vision_service.add_focus(saved_focus)
            await db.kv_set("vision_focuses", json.dumps(vision_service.get_vision_focuses()))
            logger.info("Migrated old vision_focus to new format: %s", saved_focus[:50])

    # 设置 HA 设备目录提供者
    rule_service.set_ha_catalog_provider(_get_ha_device_catalog)

    # 设置 HA 服务定义提供者
    async def _get_ha_services() -> dict:
        """获取 HA 服务定义，格式: {domain: {service: [fields]}}"""
        all_defs = await ha_service.get_service_defs(ha_client)
        return {
            domain: {svc: info["fields"] for svc, info in svcs.items()}
            for domain, svcs in all_defs.items()
        }

    rule_service.set_ha_services_provider(_get_ha_services)

    # 设置 HA 完整设备数据提供者（带 attributes，用于校验动作参数）
    rule_service.set_ha_devices_provider(ha_service.get_all_devices)

    _startup_progress.set("正在注册工具与构建智能体...")
    # 注册所有 MCP 工具（集中在 tools.py 管理）
    tool_deps = ToolDeps(
        mcp_client_manager=mcp_client_manager,
        camera_stream=camera_stream,
        vision_client=vision_client,
        ha_service=ha_service,
        ha_client_ref=_ha_client_ref,
        camera_manager=_services.get("camera_manager"),   # Task 8:多路 fallback
    )
    register_all_tools(tool_deps)

    # 所有工具已注册完毕，重新转换并重建 LangGraph Agent
    from .mcp.langchain_tools import convert_all_tools
    from .agents.langgraph_agent import build_chat_agent
    from .agents.validator_agent import ValidatorAgent
    global langgraph_agent
    langchain_tools = convert_all_tools(mcp_client_manager)
    langgraph_agent, _global_clients = build_chat_agent(tools=langchain_tools)
    _services["langgraph_agent"] = langgraph_agent
    _services["langchain_tools"] = langchain_tools

    # ── 集成插件平台启动（Dispatcher 之前，因 Dispatcher 要拿 sink_manager）──
    integration_enabled = bool(get_config("integration.enabled", False))
    integration_layer = None
    if integration_enabled:
        from pathlib import Path as _Path
        from app.integration.integration_layer import IntegrationLayer
        _plugin_dir_cfg = get_config("integration.plugin_dir", "integrations")
        # plugin_dir 相对于项目根解析（容器内工作目录 /aether）
        _plugin_dir = str(_Path("/aether") / _plugin_dir_cfg)
        integration_layer = IntegrationLayer(
            plugin_dir=_plugin_dir,
            api_version=get_config("integration.api_version", "1"),
            rpc_timeout=float(get_config("integration.default_rpc_timeout", 30.0)),
            max_restarts=int(get_config("integration.max_restarts", 3)),
        )
        try:
            await integration_layer.start()
            _container.integration_layer = integration_layer
            logger.info("集成插件平台已启动: %s",
                        [p["id"] for p in integration_layer.list_plugins() if p["alive"]])
        except Exception as exc:
            logger.error("集成插件平台启动失败（不阻塞主服务）: %s", exc)
            integration_layer = None

    # 创建 Dispatcher（使用 LangGraph Agent，传入其 httpx 客户端供生命周期管理）
    global dispatcher
    dispatcher = Dispatcher(
        session_store=session_store,
        agent=langgraph_agent,
        camera_stream=camera_stream,
        ha_catalog_provider=_get_ha_device_catalog,
        ha_controls_provider=_get_ha_device_controls,
        vision_service=vision_service,
        ha_service=ha_service,
        validator=ValidatorAgent(max_retries=1),
        summarization_service=summarization_service,
        clients=_global_clients,
        camera_manager=_services.get("camera_manager"),   # Task 9:多路
        sink_manager=integration_layer.sink_manager if integration_layer else None,
    )
    dispatcher._tools = langchain_tools  # 供 per-user agent 构建使用
    _container.dispatcher = dispatcher

    # 启动自动化评估（dhash 事件触发 + 定时器兜底，替代旧 10s 轮询 + 推理完成双触发）
    min_trigger_interval = max(0.5, float(get_config("vision.min_infer_interval_seconds", 3.0)))
    silent_eval_enabled = bool(get_config("automation.silent_eval_enabled", True))
    silent_eval_interval = max(5.0, float(get_config("automation.silent_eval_interval_seconds", 300.0)))
    _automation_agent_ref[0] = AutomationAgent(
        automation_service=automation_service,
        camera_stream=camera_stream,
        min_trigger_interval=min_trigger_interval,
        silent_eval_enabled=silent_eval_enabled,
        silent_eval_interval=silent_eval_interval,
        camera_manager=_services.get("camera_manager"),   # Task 9:多路遍历
    )
    await _automation_agent_ref[0].start()
    logger.info(
        "AutomationAgent started (min_trigger=%.1fs, silent=%s/%.1fs)",
        min_trigger_interval, silent_eval_enabled, silent_eval_interval,
    )

    # 启动定时任务调度器（与 AutomationAgent 互补：精确时刻触发，零 LLM 开销）
    scheduler_service = SchedulerService(
        db=Database.get(),
        tool_executor=tool_executor,
        dispatcher_ref=[dispatcher],  # list[0] 模式支持热替换
        session_store=session_store,
        task_manager=_background_task_mgr,
        llm_chat_client=llm_chat_client,  # reminder kind 直接调 LLM，绕开 ReAct
    )
    await scheduler_service.start()
    _container.scheduler_service = scheduler_service
    # 回填工具依赖的 ref：让 scheduled_task_* 工具能访问调度器
    tool_deps.scheduler_service_ref[0] = scheduler_service

    _startup_progress.set("正在连接摄像头与智能家居...")
    # dhash 运动触发自动化评估（事件驱动 + 定时器兜底两条入口）。
    # 旧的 on_inference_done（推理完成即触发评估）已移除：会造成与 dhash 重复评估。
    camera_stream.set_on_automation_trigger(_automation_agent_ref[0].trigger_evaluate)
    # 注入主事件循环：运动推理通过 run_coroutine_threadsafe 投到主循环跑，
    # httpx 网络等待时释放 GIL，不再像线程池那样抢 GIL 饿死采集线程（修复运动推理时 FPS 崩到 ~1）
    camera_stream.set_event_loop(asyncio.get_running_loop())
    # 注入 ONVIF 发现服务：worker 掉线连续开流失败时触发发现找回 IP
    # （discovery_service 未注入则掉线走纯指数退避，向后兼容）
    camera_stream.set_discovery_service(discovery_service)
    # 注入视觉展示开关初始状态（用户在 /camera 关过则重启后仍保持关闭）
    camera_stream.set_camera_vl_display_enabled(bool(get_config("automation.camera_vl_display_enabled", True)))
    camera_stream.start()

    # Task 7:多路 CameraManager 接线(db/ha/automation 后注入,顺序兜底)。
    # CameraManager.initialize 从 cameras 表加载所有 enabled 路,各路 worker
    # 抓帧 + 运动检测全跑;AI 预览只激活第一路 display_enabled=1(D4)。
    camera_manager = _services.get("camera_manager")
    if camera_manager is not None:
        camera_manager.set_event_loop(asyncio.get_running_loop())
        camera_manager.set_db(Database.get())
        camera_manager.set_ha_service(ha_service)
        camera_manager.set_automation_service(automation_service)
        discovery_service.set_db(Database.get())
        try:
            await camera_manager.initialize()
            logger.info("CameraManager initialized (%d stream(s))", len(camera_manager.list_cameras()))
        except Exception:
            logger.exception("CameraManager initialize failed (non-fatal, single-camera mode continues)")

    # 后台捕获摄像头 MAC（首次配对，不阻塞启动）
    # Task 7:单摄旧路径 + 多路遍历 discovery_enabled 且 device_mac 为空的路
    async def _startup_capture_mac():
        try:
            await discovery_service.capture_mac_on_startup()
        except Exception:
            logger.exception("startup MAC capture failed (legacy path)")
        # 多路:遍历所有 discovery_enabled 且 device_mac 为空的路
        if camera_manager is not None:
            try:
                for row in await Database.get().cameras_all():
                    if row.get("discovery_enabled", 1) and not str(row.get("device_mac", "")).strip():
                        try:
                            await discovery_service.capture_mac_on_startup(row["id"])
                        except Exception:
                            logger.exception("MAC capture failed for %s", row.get("id"))
            except Exception:
                logger.exception("multi-camera MAC capture failed (non-fatal)")

    _background_task_mgr.spawn(_startup_capture_mac(), name="capture_mac")

    # 后台任务
    await connect_external_mcp_servers(mcp_client_manager)
    catalog_task = asyncio.create_task(_ha_catalog_refresh_loop())

    # 启动健康检查（不阻塞启动，只记录状态）
    async def _startup_health_check():
        try:
            status = await health_checker.check_all(ha_client, llm_chat_client)
            logger.info("Startup health check: HA=%s, LLM=%s", 
                       "OK" if status["ha"] else "UNAVAILABLE",
                       "OK" if status["llm"] else "UNAVAILABLE")
        except Exception:
            logger.warning("Startup health check failed", exc_info=True)

    _background_task_mgr.spawn(_startup_health_check(), name="health_check")

    # 后台构建 RAG 索引（不阻塞启动；曾在模块级提交，现移到 lifespan 启动阶段）
    # 绑定主事件循环后再提交：RAG 向量化在后台线程内投递 async embed 调用回主循环
    rag_service.bind_loop(asyncio.get_running_loop())
    _stream_executor.submit(rag_service.safe_build)

    # 绑定语义图服务的事件循环（供 pipeline 线程内回调投递回主循环）
    _container.sg_service.bind_loop(asyncio.get_running_loop())

    _startup_progress.mark_ready()
    yield

    # 关闭
    _startup_progress.stop()
    catalog_task.cancel()
    if _automation_agent_ref[0]:
        await _automation_agent_ref[0].stop()
    if _container.scheduler_service is not None:
        await _container.scheduler_service.stop()
    # 集成插件平台停止（停止所有插件子进程）
    if _container.integration_layer is not None:
        try:
            await _container.integration_layer.stop()
            logger.info("集成插件平台已停止")
        except Exception:
            logger.exception("IntegrationLayer stop failed (non-fatal)")
    await mcp_client_manager.disconnect_all_external()
    await session_store.shutdown()
    camera_stream.stop()
    # Task 7:多路停止
    cm = _services.get("camera_manager")
    if cm is not None:
        try:
            cm.stop()
        except Exception:
            logger.exception("CameraManager stop failed (non-fatal)")
    # 回收 dispatcher 持有的所有 agent httpx 客户端（全局 + per-user），防连接池泄漏
    if dispatcher is not None:
        await dispatcher.close_all_agent_clients()
    await ha_client.close()
    await close_web_http_client()
    from .clients.llm_base_client import close_shared_client
    await close_shared_client()
    await Database.close()
    logger.info("Application shutdown")


# Dispatcher 全局引用
dispatcher: Dispatcher | None = None


# ============ 应用实例 ============

app = FastAPI(title="Aether", lifespan=lifespan)
register_exception_handlers(app)

# 注册路由模块
from .routes import settings_router, home_router, weather_router, emoji_router, advanced_router, stt_router
from .routes.auth_routes import router as auth_router
from .routes.user_routes import router as user_router
from .routes.rule_routes import router as rule_router
from .routes.scheduler_routes import router as scheduler_router
from .routes.session_routes import router as session_router
from .routes.ha_routes import router as ha_router
from .routes.mcp_routes import router as mcp_router
from .routes.ptz_routes import router as ptz_router
from .routes.discovery_routes import router as discovery_router
from .routes.camera_routes import router as camera_router
from .routes.setup_routes import router as setup_router
from .routes.doc_routes import router as doc_router
from .routes.sg_routes import router as sg_router
from .routes.integration_routes import router as integration_router
from .routes.ws_routes import router as ws_router
from .routes.automation_routes import router as automation_router
app.include_router(settings_router, prefix="/api")
app.include_router(home_router, prefix="/api")
app.include_router(weather_router, prefix="/api")
app.include_router(emoji_router, prefix="/api")
app.include_router(advanced_router, prefix="/api")
app.include_router(stt_router, prefix="/api")
app.include_router(auth_router, prefix="/api")
app.include_router(user_router, prefix="/api")
app.include_router(rule_router, prefix="/api")
app.include_router(scheduler_router, prefix="/api")
app.include_router(session_router, prefix="/api")
app.include_router(ha_router, prefix="/api")
app.include_router(mcp_router, prefix="/api")
app.include_router(ptz_router, prefix="/api")
app.include_router(discovery_router, prefix="/api")  # ONVIF 摄像头发现：/api/discovery/*
app.include_router(camera_router, prefix="/api")     # Task 6:多摄像头统一入口 /api/cameras/* + /api/ha/areas
app.include_router(automation_router, prefix="/api")  # 自动化：/api/automation/*
app.include_router(setup_router)  # 无 prefix，包含 / 和 /favicon.ico
app.include_router(doc_router)  # 路径已包含 /api 前缀或无
app.include_router(sg_router, prefix="/api")  # 语义图：/api/sg/*
app.include_router(integration_router, prefix="/api")  # 集成插件平台：/api/integrations/*
app.include_router(ws_router)  # WebSocket 路由，无 prefix

# CORS
# 安全加固：收紧 origin，仅允许本机 + 内网私有段 + Tailscale 网段。
# 原 regex 的 (\d{1,3}\.){3}\d{1,3} 会匹配任意 IPv4（含 0.0.0.0、公网 IP），过宽。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^https?://("
        r"localhost"                                   # 本机域名
        r"|127\.0\.0\.1"                               # 本机回环
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"              # 内网 10.0.0.0/8
        r"|192\.168\.\d{1,3}\.\d{1,3}"                 # 内网 192.168.0.0/16
        r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"  # 内网 172.16.0.0/12
        r"|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"  # Tailscale 100.64.0.0/10
        r")(:\d+)?$"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 可选接口令牌（向后兼容，新代码使用 JWT）
APP_TOKEN = (os.getenv("APP_TOKEN") or "").strip()


@app.middleware("http")
async def request_tracing(request: Request, call_next):
    """请求追踪 middleware：生成/传递 request_id，记录请求耗时和 metrics。"""
    # 从 header 取或生成新 request_id
    rid = request.headers.get("X-Request-ID") or new_request_id()
    set_request_id(rid)
    start = time.perf_counter()
    error = False

    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        error = response.status_code >= 500
        return response
    except Exception:
        error = True
        raise
    finally:
        elapsed = time.perf_counter() - start
        metrics_service.record_request(elapsed, error=error)
        logger.info(
            "%s %s %.3fs",
            request.method,
            request.url.path,
            elapsed,
        )
        set_request_id("-")  # 重置


@app.middleware("http")
async def api_token_guard(request, call_next):
    # 跳过 auth 路由和静态文件（非 /api 路径）
    if (request.url.path.startswith("/api/auth") or
        not request.url.path.startswith("/api")):
        return await call_next(request)

    # 检查 JWT token（header → cookie）
    from .core.auth import extract_token_from_request, verify_token
    token = extract_token_from_request(request)
    if token:
        try:
            verify_token(token)
        except Exception:
            token = None  # token 无效，落入下方 401
        else:
            # token 有效：执行路由。注意 call_next 必须在 try 之外，
            # 否则路由本身的异常会被误吞成 401。
            return await call_next(request)

    # 向后兼容：检查 APP_TOKEN（仅 header）
    if APP_TOKEN:
        provided = request.headers.get("X-API-Token")
        if provided == APP_TOKEN:
            return await call_next(request)

    return JSONResponse(
        status_code=401,
        content=ApiResponse(code="unauthorized", message="未认证，请先登录", data=None).model_dump(),
    )


@app.middleware("http")
async def global_rate_limit(request: Request, call_next):
    """全局速率限制：按 client IP 限流，防止内网滥用 LLM API。

    豁免：WebSocket(/ws/*)、静态资源(非 /api 路径)、auth 路由(已有独立 limiter)。
    阈值 120 次/分钟，正常使用不触发。
    """
    path = request.url.path
    # 豁免 WebSocket（长连接，HTTP 中间件会误断）、非 API 静态资源、auth 路由
    if (path.startswith("/ws/") or
        not path.startswith("/api") or
        path.startswith("/api/auth")):
        return await call_next(request)

    client_ip = request.client.host if request.client else "unknown"
    if not global_limiter.check(client_ip):
        logger.warning("Rate limited: %s %s from %s", request.method, path, client_ip)
        return JSONResponse(
            status_code=429,
            content=ApiResponse(
                code="rate_limited",
                message="请求过于频繁，请稍后再试",
                data=None,
            ).model_dump(),
        )
    return await call_next(request)


# ============ 系统状态路由 ============


def _primary_camera_state() -> dict:
    """Task 10:取主摄像头状态(第一个 enabled)。camera_manager 为空回退 camera_stream。

    全局 health/state 端点用此保持兼容 —— 返回主摄像头状态,/camera 弹窗外
    的前端引用不崩。manager 未 initialize 时返回空 CameraState。
    """
    cm = _services.get("camera_manager")
    if cm is not None:
        cams = cm.list_cameras()
        if cams:
            return cm.get_state(cams[0]["id"])
        return {"camera_id": "", "camera_opened": False, "backend_name": "unknown",
                "frame_width": 0, "frame_height": 0, "fps": 0.0, "last_frame_at": 0.0,
                "last_error": None, "action": "idle", "feedback": "", "details": None,
                "confirmed": False}
    return camera_stream.get_state()


@app.get("/api/health")
async def health() -> ApiResponse[HealthData]:
    state = CameraStateModel.model_validate(_primary_camera_state())
    health_status = health_checker.get_status()
    return ApiResponse(
        data=HealthData(
            status="ok",
            llm_model=llm_chat_client.model,
            llm_enabled=llm_chat_client.enabled,
            camera=state,
            log_file=str(LOG_FILE),
            ha_available=health_status["ha_available"],
            llm_available=health_status["llm_available"],
        )
    )


@app.get("/api/metrics")
async def metrics() -> ApiResponse[dict]:
    """返回内存指标快照：请求计数、延迟、工具调用、LLM 调用等。"""
    return ApiResponse(data=metrics_service.snapshot())


@app.get("/api/state")
async def state() -> ApiResponse[CameraStateModel]:
    current_state = _primary_camera_state()   # Task 10:主摄像头
    return ApiResponse(data=CameraStateModel.model_validate(current_state))


# ============ RAG 文档助手 ============
# RAG 索引状态与操作已收敛到 RagService（app/services/rag_service.py），
# 由 AppContainer 持有。索引在 lifespan 启动阶段后台构建（见 _stream_executor.submit）。
# RAG_CHUNKS / RAG_FAISS_INDEX / RAG_EMBEDDER 全局变量已移除，路由通过 container.rag_service 访问。


# ============ Vue 前端静态文件服务 ============

from fastapi.staticfiles import StaticFiles

FRONTEND_DIR = STATIC_DIR / "frontend"

# 挂载 Vue 构建产物的 assets 目录
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="frontend-assets")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """SPA fallback: 非 /api、/ws 的请求返回 Vue index.html。"""
    if full_path.startswith("api/") or full_path.startswith("ws/"):
        return JSONResponse(status_code=404, content={"error": "not found"})

    # 安全校验：resolve 后必须仍在 FRONTEND_DIR 内，防止 .. 路径穿越
    frontend_root = FRONTEND_DIR.resolve()
    file_path = (FRONTEND_DIR / full_path).resolve()
    try:
        file_path.relative_to(frontend_root)
    except ValueError:
        return JSONResponse(status_code=404, content={"error": "not found"})

    if file_path.is_file():
        return FileResponse(file_path)

    index_path = FRONTEND_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)

    return RedirectResponse(url="/landing")
