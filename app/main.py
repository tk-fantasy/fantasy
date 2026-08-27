"""Aether 应用入口。"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
from contextlib import asynccontextmanager
from .core.log_rotate import CopyTruncateRotatingFileHandler
from pathlib import Path

# 启动进度上报：先于任何重依赖导入，起一个轻量进度端口（8011），
# 让加载页在冷启动期间能拿到真实的加载阶段（主端口 8010 此时尚未监听）。
from .startup_progress import startup_progress as _startup_progress
_startup_progress.start()
_startup_progress.set("正在加载后端依赖...")

import faiss
import numpy as np
import openai

from fastapi import FastAPI, WebSocket, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse, RedirectResponse

from .agents.automation_agent import AutomationAgent
from .agents.dispatcher import Dispatcher
from .bootstrap import initialize_services
from .container import AppContainer, get_container, init_container
from .core import ApiResponse, CameraStateModel, Database, HealthData
from .core.config import get_config
from .core.rate_limit import global_limiter
from .core.tracing import RequestIdFilter, new_request_id, set_request_id
from .core.version import get_version
from .mcp.web_tools import close_http_client as close_web_http_client
from .migrations import load_vision_focuses, migrate_camera_frame_interval, migrate_global_llm_keys, migrate_home_info
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
        # copy-truncate 轮转：logs/ bind-mount 到 Windows 宿主时 rename 会被
        # 文件共享层拒绝（宿主持有句柄），标准 RotatingFileHandler 轮转失败后
        # 文件日志停写。详见 app/core/log_rotate.py。
        CopyTruncateRotatingFileHandler(
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
# LangGraph Agent：lifespan 内构建（见 _rebuild_agent / dispatcher.set_agent 唯一通道）
langgraph_agent = None
ha_client = _services["ha_client"]
ha_service = _services["ha_service"]
_automation_agent_ref = _services["automation_agent_ref"]
_host_integrations_ref: list = []  # 宿主侧集成实例列表（通用加载，不含具体插件名）
_ha_catalog_cache_ref = _services["ha_catalog_cache_ref"]
_ha_client_ref = _services["ha_client_ref"]
discovery_service = _services["discovery_service"]

# Metrics 服务（轻量内存计数器）
metrics_service = MetricsService()

# 健康检查器（跟踪外部服务可用性）
health_checker = HealthChecker()

# 初始化 DI 容器
_container = init_container(_services, metrics_service)
# 与 catalog 同源：复用容器创建的 controls 缓存 ref（catalog 走 bootstrap→services，
# controls 走容器默认值，这里不再新建覆盖，保持两条 ref 路径对称）
_ha_controls_cache_ref = _container.ha_controls_cache_ref
# 注入 catalog 刷新回调：set_entity_note 写完备注立即触发，
# 让新备注进 _ha_controls_cache_ref，不必等后台 60 秒循环。
# 用 lambda 延迟引用 _refresh_ha_catalog（它在模块后部定义，调用时才解析）。
_container.catalog_refresh_fn = lambda: _refresh_ha_catalog()
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
    if dispatcher is not None:
        await dispatcher.set_agent(new_agent, tools=langchain_tools, clients=new_clients)
    logger.info("Agent rebuilt with %d tools", len(langchain_tools))


def sync_ha_runtime_refs(new_client, new_service) -> None:
    """ha_routes 保存 HA 配置热替换 client/service 后，同步各持有旧对象的组件。

    覆盖三类持有点：
    - main 模块全局（刷新循环/服务定义闭包按名字查全局，重新赋值即生效）；
    - 构造期捕获旧对象的组件：dispatcher / rule_service 设备提供者 /
      集成 host_deps（经 update_ha_refs 重绑）/ ToolDeps.ha_service；
    - camera_manager（已有 set_ha_service 通道）。
    不做这一步，改一次 HA 配置后旧 client 已 close，目录刷新、设备名映射、
    插件反向 call_service 等会持续失败直到重启进程。
    """
    global ha_client, ha_service
    ha_client = new_client
    ha_service = new_service
    rule_service.set_ha_devices_provider(new_service.get_all_devices)
    if dispatcher is not None:
        dispatcher.set_ha_service(new_service)
    il = _container.integration_layer if _container is not None else None
    if il is not None:
        il.update_ha_refs(new_client, new_service)
    cm = _services.get("camera_manager")
    if cm is not None:
        cm.set_ha_service(new_service)
    td = getattr(_container, "tool_deps", None)
    if td is not None:
        td.ha_service = new_service


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

    # 向后兼容：检查 APP_TOKEN（compare_digest 防时序侧信道）
    if APP_TOKEN:
        import secrets

        provided = websocket.headers.get("X-API-Token") or websocket.query_params.get("app_token")
        if provided and secrets.compare_digest(provided, APP_TOKEN):
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
    """后台刷新 HA 设备目录缓存（catalog + controls 双缓存，60 秒循环）。

    数据源收敛到 device_registry.build_device_snapshot —— 渲染给 AI 的视图与
    call_service 拒绝时的候选反查共用同一快照，消除「闸门认识、视图不认识」的
    漂移。catalog 行格式 `- entity_id (类型:domain, 状态:xxx) 名称:xxx` 是
    rule_service._parse_ha_catalog 的正则契约（名称分组为 (.+)，容忍子功能名）。
    禁止设备（entity_operable 黑名单）在渲染层直接排除，对 AI 不可见。
    """
    try:
        from .services.device_registry import (
            build_device_snapshot, render_catalog_text, render_controls_text,
        )
        snapshot = await build_device_snapshot(ha_service, ha_client)
        _ha_catalog_cache_ref[0] = render_catalog_text(snapshot)
        _ha_controls_cache_ref[0] = render_controls_text(snapshot)
    except Exception:  # noqa: BLE001
        logger.warning("HA catalog refresh failed", exc_info=True)


async def _ha_catalog_refresh_loop() -> None:
    """后台定时刷新 HA 设备目录缓存(每 60 秒)。

    _refresh_ha_catalog 内部已吞掉业务异常，循环里只需处理取消。
    """
    await asyncio.sleep(5)
    while True:
        try:
            await _refresh_ha_catalog()
        except asyncio.CancelledError:
            break
        await asyncio.sleep(60)


# ============ 生命周期 ============


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期管理。"""

    logger.info(
        "Application startup",
        extra={"llm_enabled": llm_chat_client.enabled, "llm_model": llm_chat_client.model},
    )

    # 全局令牌旁路是"全家共享后门"：设置后任何持令牌者可绕过 JWT 与用户
    # 隔离直接访问全部 API。当前部署未使用，一旦被设置必须让人看见。
    if (os.getenv("APP_TOKEN") or "").strip():
        logger.warning("APP_TOKEN 已设置：X-API-Token 可绕过用户认证访问全部 API，"
                       "确认这是有意为之，否则从 .env 移除")

    _startup_progress.set("正在初始化数据库...")
    # 初始化数据库
    await Database.init()

    # 异步加载 emoji 索引（不阻塞启动）
    _background_task_mgr.spawn(emoji_service.load_index_async(), name="emoji_index_load")

    # 升级包自动升级监视器：backups/ 出现更高版本的包即自动安装
    from .ops.auto_update import watcher_loop as _auto_update_loop
    _background_task_mgr.spawn(_auto_update_loop(), name="auto_update_watcher")

    _startup_progress.set("正在加载会话与规则...")
    # 从数据库加载持久化数据
    await rule_registry_service.load_from_db()
    await session_store.load_from_db()

    # 启动期一次性历史数据迁移（详见 app/migrations.py）：DB 是兼容性 fallback，
    # config.json 是新真源。单块失败只 warning，不阻塞启动。
    db = Database.get()
    await migrate_global_llm_keys(db)

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

    await migrate_home_info(db)

    await migrate_camera_frame_interval(db)

    await load_vision_focuses(db, vision_service)

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
        vision_client=vision_client,
        ha_service=ha_service,
        ha_client_ref=_ha_client_ref,
        camera_manager=_services.get("camera_manager"),   # 唯一摄像头来源(多路)
    )
    register_all_tools(tool_deps)
    # 挂到容器：sync_ha_runtime_refs 热替换 HA service 时同步 ToolDeps 持有的引用
    _container.tool_deps = tool_deps

    # 所有工具已注册完毕，重新转换并重建 LangGraph Agent
    from .mcp.langchain_tools import convert_all_tools
    from .agents.langgraph_agent import build_chat_agent
    from .agents.validator_agent import ValidatorAgent
    global langgraph_agent
    langchain_tools = convert_all_tools(mcp_client_manager)
    langgraph_agent, _global_clients = build_chat_agent(tools=langchain_tools)

    # ── 集成插件平台启动（Dispatcher 之前，因 Dispatcher 要拿 sink_manager）──
    integration_enabled = bool(get_config("integration.enabled", False))
    integration_layer = None
    if integration_enabled:
        from pathlib import Path as _Path
        from app.integration.integration_layer import IntegrationLayer
        from app.integration.config_helper import get_broadcast_enabled
        _plugin_dir_cfg = get_config("integration.plugin_dir", "integrations")
        # plugin_dir 相对于项目根解析（容器内工作目录 /aether）
        _plugin_dir = str(_Path("/aether") / _plugin_dir_cfg)
        # host_deps：暴露宿主能力给插件反向调用（Phase 3 方向 2）。
        # 凭证不再注入子进程环境变量——插件经 host.ha.call_service 反向 RPC 操作设备。
        integration_layer = IntegrationLayer(
            plugin_dir=_plugin_dir,
            api_version=get_config("integration.api_version", "1"),
            rpc_timeout=float(get_config("integration.default_rpc_timeout", 30.0)),
            max_restarts=int(get_config("integration.max_restarts", 3)),
            broadcast_enabled=get_broadcast_enabled(),
            host_deps={"ha_client": ha_client, "ha_service": ha_service,
                       "llm_chat_client": llm_chat_client,
                       "camera_manager": _services.get("camera_manager")},
        )
        try:
            await integration_layer.start()
            _container.integration_layer = integration_layer
            logger.info("集成插件平台已启动: %s (广播=%s)",
                        [p["id"] for p in integration_layer.list_plugins() if p["alive"]],
                        integration_layer.sink_manager.broadcast_enabled)
        except Exception as exc:
            logger.error("集成插件平台启动失败（不阻塞主服务）: %s", exc)
            integration_layer = None

    # 创建 Dispatcher（使用 LangGraph Agent，传入其 httpx 客户端供生命周期管理）
    global dispatcher
    dispatcher = Dispatcher(
        session_store=session_store,
        agent=langgraph_agent,
        ha_catalog_provider=_get_ha_device_catalog,
        ha_controls_provider=_get_ha_device_controls,
        catalog_refresh_fn=_refresh_ha_catalog,  # controls 空时同步刷新,确保备注不缺位
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

    # 启动自动化评估（dhash 事件触发(仅视觉) + 视觉/非视觉双静默兜底）
    silent_eval_enabled = bool(get_config("automation.silent_eval_enabled", True))
    silent_eval_interval = max(5.0, float(get_config("automation.silent_eval_interval_seconds", 300.0)))
    nonvision_silent_enabled = bool(get_config("automation.nonvision_silent_enabled", True))
    nonvision_silent_interval = max(5.0, float(get_config("automation.nonvision_silent_interval_seconds", 30.0)))
    _automation_agent_ref[0] = AutomationAgent(
        automation_service=automation_service,
        silent_eval_enabled=silent_eval_enabled,
        silent_eval_interval=silent_eval_interval,
        camera_manager=_services.get("camera_manager"),
        nonvision_silent_enabled=nonvision_silent_enabled,
        nonvision_silent_interval=nonvision_silent_interval,
    )
    await _automation_agent_ref[0].start()
    logger.info(
        "AutomationAgent started (vision-silent=%s/%.1fs, nonvision-silent=%s/%.1fs)",
        silent_eval_enabled, silent_eval_interval,
        nonvision_silent_enabled, nonvision_silent_interval,
    )

    # 启动定时任务调度器（与 AutomationAgent 互补：精确时刻触发，零 LLM 开销）
    scheduler_service = SchedulerService(
        db=Database.get(),
        tool_executor=tool_executor,
        dispatcher_ref=[dispatcher],  # list[0] 模式支持热替换
        session_store=session_store,
        task_manager=_background_task_mgr,
        llm_chat_client=llm_chat_client,  # reminder kind 直接调 LLM，绕开 ReAct
        sink_manager=integration_layer.sink_manager if integration_layer else None,  # reminder 语音广播
    )
    await scheduler_service.start()
    _container.scheduler_service = scheduler_service
    # 回填工具依赖的 ref：让 scheduled_task_* 工具能访问调度器
    tool_deps.scheduler_service_ref[0] = scheduler_service

    _startup_progress.set("正在连接摄像头与智能家居...")
    # 多路 CameraManager 是唯一摄像头来源:各路 worker 自带 dhash 运动检测 +
    # _on_automation_trigger 事件驱动评估(自闭环,不经 AutomationAgent.trigger_evaluate)。
    # AutomationAgent 只剩定时器兜底(_silent_tick_loop 遍历各路 evaluate)。

    # Task 7:多路 CameraManager 接线(db/ha/automation 后注入,顺序兜底)。
    # CameraManager.initialize 从 cameras 表加载所有 enabled 路,各路 worker
    # 抓帧 + 运动检测全跑;AI 预览只激活第一路 display_enabled=1(D4)。
    camera_manager = _services.get("camera_manager")
    if camera_manager is not None:
        camera_manager.set_event_loop(asyncio.get_running_loop())
        camera_manager.set_db(Database.get())
        camera_manager.set_ha_service(ha_service)
        camera_manager.set_automation_service(automation_service)
        automation_service.set_camera_manager(camera_manager)
        discovery_service.set_db(Database.get())
        try:
            await camera_manager.initialize()
            # 应用全局预览开关初始状态(用户在 /camera 关过则重启后仍保持关闭)
            if not bool(get_config("automation.camera_vl_display_enabled", True)):
                camera_manager.set_camera_vl_display_enabled(False)
            logger.info("CameraManager initialized (%d stream(s))", len(camera_manager.list_cameras()))
        except Exception:
            logger.exception("CameraManager initialize failed (non-fatal)")

    # 后台捕获摄像头 MAC（首次配对，不阻塞启动）
    # Task 7:多路遍历 discovery_enabled 且 device_mac 为空的路（写回 cameras 表，
    # 表是唯一真源；旧的全局 config.json 单摄路径已随多路化移除）
    async def _startup_capture_mac():
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

    # 周期健康复检：此前只在启动时探一次，运行期 HA 挂掉/恢复完全无感知，
    # /api/health 一直是启动时刻的冻结值。HA 探测是本地 REST 调用零成本，
    # 每 60s 一次；LLM 探测要真实消耗 token，默认关闭
    # （health.llm_interval_seconds 配 300 等值可开启）。
    async def _periodic_health_loop():
        ha_every = max(15, int(get_config("health.ha_interval_seconds", 60)))
        llm_every = int(get_config("health.llm_interval_seconds", 0))
        ticks = 0
        while True:
            await asyncio.sleep(ha_every)
            ticks += 1
            try:
                await health_checker.check_ha(ha_client)
                if llm_every > 0 and ticks % max(1, llm_every // ha_every) == 0:
                    await health_checker.check_llm(llm_chat_client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("periodic health check failed", exc_info=True)

    _background_task_mgr.spawn(_periodic_health_loop(), name="periodic_health")

    # 后台构建 RAG 索引（不阻塞启动；曾在模块级提交，现移到 lifespan 启动阶段）
    # 绑定主事件循环后再提交：RAG 向量化在后台线程内投递 async embed 调用回主循环
    rag_service.bind_loop(asyncio.get_running_loop())
    _stream_executor.submit(rag_service.safe_build)

    # 绑定语义图服务的事件循环（供 pipeline 线程内回调投递回主循环）
    _container.sg_service.bind_loop(asyncio.get_running_loop())

    # ── 宿主侧集成（通用扫描 integrations/*/main.py，不硬编码插件名）──
    _host_integrations_ref[:] = _start_host_integrations(_container, asyncio.get_running_loop())
    # 供插件配置 API 热重启单个宿主集成（改配置 → stop+start，无需重启容器）
    _container.restart_host_integration_fn = _restart_host_integration

    _startup_progress.mark_ready()
    yield

    # 关闭
    _startup_progress.stop()
    if catalog_task is not None:
        catalog_task.cancel()

    async def _safe_stop(label: str, fn) -> None:
        """逐项停机：任一失败不得跳过其余清理（此前异常会中断整条停机链，
        session pending 写入、aiosqlite 连接等后续步骤全部跳过）。"""
        try:
            await fn()
            logger.info("%s stopped", label)
        except Exception:
            logger.exception("%s stop failed (non-fatal)", label)

    # 宿主侧集成停止（通用，不硬编码插件名）——内部自带逐项 try/except，
    # 包一层 async 以统一走 _safe_stop
    if _host_integrations_ref:
        async def _stop_hosts():
            _stop_host_integrations(_host_integrations_ref)
        await _safe_stop("host integrations", _stop_hosts)
    if _automation_agent_ref[0]:
        await _safe_stop("automation agent", _automation_agent_ref[0].stop)
    if _container.scheduler_service is not None:
        await _safe_stop("scheduler", _container.scheduler_service.stop)
    # 集成插件平台停止（停止所有插件子进程）
    if _container.integration_layer is not None:
        await _safe_stop("integration layer", _container.integration_layer.stop)
    await _safe_stop("external MCP servers", mcp_client_manager.disconnect_all_external)
    await _safe_stop("session store", session_store.shutdown)
    # 多路停止
    cm = _services.get("camera_manager")
    if cm is not None:
        await _safe_stop("camera manager", asyncio.to_thread(cm.stop))
    # 回收 dispatcher 持有的所有 agent httpx 客户端（全局 + per-user），防连接池泄漏
    if dispatcher is not None:
        await _safe_stop("dispatcher clients", dispatcher.close_all_agent_clients)
        if dispatcher._validator is not None:
            await _safe_stop("validator clients", dispatcher._validator.close_all)
    await _safe_stop("HA client", ha_client.close)
    await _safe_stop("web http client", close_web_http_client)
    from .clients.llm_base_client import close_shared_client
    await _safe_stop("shared LLM client", close_shared_client)
    await _safe_stop("database", Database.close)
    _reset_global_state()
    logger.info("Application shutdown")


# Dispatcher 全局引用
dispatcher: Dispatcher | None = None


def _reset_global_state() -> None:
    """清回 lifespan 期间注入的进程级全局运行时对象。

    shutdown 末尾调用，避免进程内重启（uvicorn --reload / 测试复用进程）时
    ``dispatcher`` 等仍指向已关闭的旧对象（僵尸）。agent 的 httpx 客户端已在此前
    由 ``dispatcher.close_all_agent_clients()`` 回收，这里只解除引用。
    """
    global dispatcher, langgraph_agent  # noqa: PLW0603
    dispatcher = None
    langgraph_agent = None
    _container.dispatcher = None


# ============ 应用实例 ============

app = FastAPI(title="Aether", lifespan=lifespan)
register_exception_handlers(app)

# 注册路由模块
from .routes import llm_key_router, global_config_router, home_router, weather_router, emoji_router, advanced_router, stt_router
from .routes.auth_routes import router as auth_router
from .routes.user_routes import router as user_router
from .routes.rule_routes import router as rule_router
from .routes.scheduler_routes import router as scheduler_router
from .routes.session_routes import router as session_router
from .routes.ha_routes import router as ha_router
from .routes.mcp_routes import router as mcp_router
from .routes.camera_routes import router as camera_router
from .routes.vision_log_routes import router as vision_log_router
from .routes.setup_routes import router as setup_router
from .routes.doc_routes import router as doc_router
from .routes.sg_routes import router as sg_router
from .routes.integration_routes import router as integration_router
from .routes.ws_routes import router as ws_router
from .routes.automation_routes import router as automation_router
from .routes.simulator_routes import router as simulator_router
from .routes.ops_routes import router as ops_router
app.include_router(llm_key_router, prefix="/api")
app.include_router(global_config_router, prefix="/api")
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
app.include_router(camera_router, prefix="/api")     # Task 6:多摄像头统一入口 /api/cameras/* + /api/ha/areas
app.include_router(vision_log_router, prefix="/api")  # 识别日志：/api/vision-logs
app.include_router(automation_router, prefix="/api")  # 自动化：/api/automation/*
app.include_router(simulator_router, prefix="/api")  # 虚拟设备开关：/api/simulator/*
app.include_router(setup_router)  # 无 prefix，包含 / 和 /favicon.ico
app.include_router(doc_router)  # 路径已包含 /api 前缀或无
app.include_router(sg_router, prefix="/api")  # 语义图：/api/sg/*
app.include_router(integration_router, prefix="/api")  # 集成插件平台：/api/integrations/*
app.include_router(ops_router, prefix="/api")  # 运维：诊断包导出 /api/ops/*
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
async def api_token_guard(request, call_next):
    # 跳过 auth 路由和静态文件（非 /api 路径）
    if (request.url.path.startswith("/api/auth") or
        not request.url.path.startswith("/api")):
        return await call_next(request)

    # CORS 预检放行：本中间件在 CORSMiddleware 外层（后注册者在外），不带
    # 凭据的 OPTIONS 请求若在此 401，响应缺少 CORS 头，浏览器跨域直接失败。
    # 预检本身无业务语义，放行给内层的 CORSMiddleware 应答即可。
    if request.method == "OPTIONS":
        return await call_next(request)

    # 检查 JWT token（header → cookie）
    from .core.auth import extract_token_from_request, verify_token
    token = extract_token_from_request(request)
    if token:
        try:
            payload = verify_token(token)
        except Exception:
            token = None  # token 无效，落入下方 401
        else:
            # refresh token 不能访问 API（只能用于 /api/auth/refresh）。
            # get_current_user 依赖已有此校验，但 13 个路由文件未挂该依赖，
            # 只靠本中间件守门——故中间件必须同样拒绝 refresh token，否则
            # 持 refresh token 即可访问摄像头/HA/自动化等全部控制面。
            if payload.get("type") != "access":
                token = None
            else:
                # token 有效：执行路由。注意 call_next 必须在 try 之外，
                # 否则路由本身的异常会被误吞成 401。
                return await call_next(request)

    # 向后兼容：检查 APP_TOKEN（仅 header，compare_digest 防时序侧信道）
    if APP_TOKEN:
        import secrets

        provided = request.headers.get("X-API-Token")
        if provided and secrets.compare_digest(provided, APP_TOKEN):
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


# 请求追踪 middleware：生成/传递 request_id，记录请求耗时和 metrics。
# 注册顺序=代码顺序，FastAPI 后注册者在最外层——tracing 必须最后注册，
# 否则被限流(429)/鉴权(401)拒绝的响应不经过 tracing：无 X-Request-ID 回写头、
# 不进 metrics、日志无 request_id，客户端无法关联被拒请求。
@app.middleware("http")
async def request_tracing(request: Request, call_next):
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


# ============ 系统状态路由 ============


def _build_dispatch_fn(dispatcher):
    """构造宿主通用的 dispatch 适配函数（供宿主侧集成插件调 LLM）。"""
    from .schema.chat_schema import Event, Nlp
    from .core.tracing import new_request_id
    import logging
    logger = logging.getLogger(__name__)

    async def _dispatch(query: str, session_id: str, user_id: str) -> str:
        rid = new_request_id()
        event = Event.build_event(
            Nlp.Request(query=query),
            request_id=rid,
            session_id=session_id,
        )
        try:
            instructions = await dispatcher.dispatch(event, user_id=user_id)
            for inst in instructions:
                header = getattr(inst, "header", None)
                payload = getattr(inst, "payload", None) or {}
                ns = getattr(header, "namespace", "") if header else ""
                name = getattr(header, "name", "") if header else ""
                if ns == "Template" and name == "ToastStream":
                    return payload.get("stream", "") or ""
            return ""
        except Exception as exc:
            logger.warning("集成 dispatch 失败: %s", exc)
            return "抱歉，处理消息时出错了。"

    return _dispatch


def _start_host_integrations(container, loop):
    """通用宿主侧集成加载：扫描 integrations/*/main.py，调 start(dispatch_fn, loop)。

    不硬编码任何插件名。每个宿主侧集成在 integrations/<name>/main.py 定义
    start(dispatch_fn, loop) -> instance | None 和 stop()。
    删目录 → 找不到 → 跳过 → 零影响。
    成功启动的集成都注册到 IntegrationLayer，供插件管理页显示。
    """
    import importlib.util

    integrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "integrations")
    if not os.path.isdir(integrations_dir):
        return []

    dispatch_fn = _build_dispatch_fn(container.dispatcher)
    started = []

    # 每个集成可以有一个 meta.py 声明显示信息（name/description/capabilities）
    for name in sorted(os.listdir(integrations_dir)):
        main_path = os.path.join(integrations_dir, name, "main.py")
        if not os.path.isfile(main_path):
            continue
        try:
            mod_name = f"integrations.{name}.main"
            spec = importlib.util.spec_from_file_location(mod_name, main_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "start"):
                instance = mod.start(dispatch_fn, loop)
                if instance:
                    started.append((name, mod, instance))
                    logger.info("宿主侧集成 %s 已启动", name)
                    # 注册到 IntegrationLayer 供插件管理页显示
                    meta = _load_host_integration_meta(name, integrations_dir)
                    if container.integration_layer:
                        container.integration_layer.register_host_integration(name, meta)
        except Exception:
            logger.exception("宿主侧集成 %s 加载失败（non-fatal）", name)

    return started


def _load_host_integration_meta(name: str, integrations_dir: str) -> dict:
    """加载宿主侧集成的显示元信息（从 meta.py 或目录名推断）。"""
    meta_path = os.path.join(integrations_dir, name, "meta.py")
    default_meta = {
        "name": name,
        "version": "",
        "description": f"宿主侧集成",
        "capabilities": [],
        "alive": True,
    }
    if not os.path.isfile(meta_path):
        return default_meta
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(f"integrations.{name}.meta", meta_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return {
            "name": getattr(mod, "NAME", name),
            "version": getattr(mod, "VERSION", ""),
            "description": getattr(mod, "DESCRIPTION", f"宿主侧集成"),
            "capabilities": getattr(mod, "CAPABILITIES", []),
            # 声明了 CONFIG_SCHEMA 的宿主集成会在插件管理页弹窗里渲染配置表单
            "config_schema": getattr(mod, "CONFIG_SCHEMA", {}),
            "alive": True,
        }
    except Exception:
        return default_meta


def _restart_host_integration(name: str, loop=None) -> bool:
    """热重启一个宿主侧集成（改配置后调用）：stop → 重新 start。

    Returns:
        True=重启成功或该集成本就未运行（凭证缺失时 start 返回 None 属正常）；
        False=找不到该集成。
    """
    for idx, (iname, mod, _instance) in enumerate(_host_integrations_ref):
        if iname != name:
            continue
        try:
            if hasattr(mod, "stop"):
                mod.stop()
        except Exception:
            logger.exception("宿主侧集成 %s 停止失败（继续尝试重启）", name)
        instance = None
        if hasattr(mod, "start"):
            # dispatch_fn 用当前容器 dispatcher 重建（与启动时同源）
            dispatch_fn = _build_dispatch_fn(_container.dispatcher)
            instance = mod.start(dispatch_fn, loop or asyncio.get_event_loop())
        _host_integrations_ref[idx] = (iname, mod, instance)
        # 同步插件管理页的存活状态
        meta = _load_host_integration_meta(
            name, os.path.join(os.path.dirname(os.path.dirname(__file__)), "integrations"))
        if _container.integration_layer:
            meta["alive"] = instance is not None
            _container.integration_layer.register_host_integration(name, meta)
        logger.info("宿主侧集成 %s 已热重启（alive=%s）", name, instance is not None)
        return True
    return False


def _stop_host_integrations(started_list):
    """停止所有宿主侧集成。"""
    for name, mod, instance in started_list:
        try:
            if hasattr(mod, "stop"):
                mod.stop()
                logger.info("宿主侧集成 %s 已停止", name)
        except Exception:
            logger.exception("宿主侧集成 %s 停止失败（non-fatal）", name)


def _primary_camera_state() -> dict:
    """取主摄像头状态(CameraManager.primary_camera_id:预览路优先)。

    全局 health 端点用此填充 camera 字段 —— manager 未 initialize 或
    无路时返回空 CameraState。
    """
    cm = _services.get("camera_manager")
    if cm is None:
        return dict(Dispatcher.EMPTY_CAMERA_STATE)
    getter = getattr(cm, "primary_camera_id", None)
    cid = getter() if callable(getter) else None
    if not cid:
        return dict(Dispatcher.EMPTY_CAMERA_STATE)
    return cm.get_state(cid)


_APP_START_MONOTONIC = time.monotonic()


@app.get("/healthz")
async def healthz() -> dict:
    """无认证存活探针（docker healthcheck 专用）。

    刻意不用 /api 前缀——api_token_guard 守 /api/*，探活拿不到 JWT。
    只返回极简信息；事件循环卡死时本请求无法被处理，docker 探活超时
    判定 unhealthy 触发重启——这是单 loop 架构下"进程活着但全站冻结"
    唯一的自愈路径。
    """
    return {
        "status": "ok",
        "uptime_seconds": round(time.monotonic() - _APP_START_MONOTONIC),
        "version": get_version(),
    }


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
            version=get_version(),
        )
    )


@app.get("/api/metrics")
async def metrics() -> ApiResponse[dict]:
    """返回内存指标快照：请求计数、延迟、工具调用、LLM 调用等。"""
    return ApiResponse(data=metrics_service.snapshot())


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
