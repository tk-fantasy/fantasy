"""应用依赖注入容器。

所有服务通过 AppContainer 访问，消除 `from ..main import xxx` 延迟导入模式。
路由通过 `Depends(get_container)` 获取容器实例。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AppContainer:
    """应用服务容器。

    稳定服务：启动后不替换实例。
    可变引用：通过 list[0] 模式支持运行时热替换（如 HA 客户端）。
    """

    # ── 客户端 ──
    ha_service: Any  # HAService
    llm_chat_client: Any  # LlmChatClient
    vision_client: Any  # LlmVisionClient
    summary_client: Any  # LlmChatClient (role=summary)
    embed_client: Any  # LlmChatClient (role=embed)

    # ── 服务 ──
    session_store: Any  # SessionStore
    vision_service: Any  # VisionService
    vision_key_pool: Any  # ApiKeyManager
    rule_service: Any  # RuleService
    rule_registry_service: Any  # RuleRegistryService
    automation_service: Any  # AutomationService
    summarization_service: Any  # SummarizationService
    llm_settings_service: Any  # LlmSettingsService
    emoji_service: Any  # EmojiService
    metrics_service: Any  # MetricsService

    # ── MCP / 工具 ──
    mcp_client_manager: Any  # MCPClientManager
    tool_executor: Any  # ToolExecutor

    # ── 摄像头 ──
    camera_stream: Any  # CameraStream(过渡保留,Task 9-10 切到 camera_manager 后删)

    # ── 可变引用（支持热替换）──
    ha_client_ref: list  # [HomeAssistantClient] — set_ha_config 时替换
    automation_agent_ref: list  # [AutomationAgent | None]
    ha_catalog_cache_ref: list  # [str] — HA 设备目录缓存
    ha_controls_cache_ref: list  # [str] — HA 控件文本缓存
    # catalog 刷新回调（main.py 注入 _refresh_ha_catalog）。
    # set_entity_note 写完备注后立即触发一次，让新备注进缓存，
    # 不必等后台 60 秒循环（否则用户写完备注立刻聊天，LLM 看到的还是旧缓存）。
    catalog_refresh_fn: Any = None

    # ── 调度器（lifespan 启动阶段赋值，初始化前为 None）──
    # 放在所有无默认值字段之后，因其有默认值。
    camera_manager: Any = None  # Task 7:CameraManager(多路)
    discovery_service: Any = None  # Task 6:ONVIF 发现(模块单例,容器暴露给路由)
    ptz_registry: Any = None  # Task 5:PtzRegistry(模块单例,容器暴露给路由)
    dispatcher: Any = None  # Dispatcher | None
    scheduler_service: Any = None  # SchedulerService | None

    # ── RAG 文档助手（lifespan 启动阶段后台构建索引）──
    rag_service: Any = None  # RagService | None

    # ── 语义图构建服务（按需触发，复用 embed/chat 客户端）──
    sg_service: Any = None  # SemanticGraphService | None

    # ── 集成插件平台（lifespan 启动阶段赋值）──
    integration_layer: Any = None  # IntegrationLayer | None

    # ── 便捷属性（动态读取当前实例）──
    @property
    def ha_client(self) -> Any:
        """动态读取当前 HA 客户端（支持热替换）。"""
        return self.ha_client_ref[0]

    def reload_all_clients(self) -> None:
        """重载所有 LLM 客户端（切换用户或更新 key 后调用）。"""
        self.llm_chat_client.reload()
        self.vision_client.reload()
        self.summary_client.reload()
        self.embed_client.reload()
        if self.rag_service:
            self.rag_service.maybe_rebuild_if_model_changed()


# 全局容器实例
_container: AppContainer | None = None


def get_container() -> AppContainer:
    """获取全局容器实例。FastAPI Depends 使用。"""
    if _container is None:
        raise RuntimeError("AppContainer not initialized. Call init_container() first.")
    return _container


def init_container(services: dict[str, Any], metrics_service: Any) -> AppContainer:
    """从 services dict 初始化全局容器。

    Args:
        services: bootstrap.initialize_services() 返回的服务字典
        metrics_service: MetricsService 实例（在 main.py 中创建）

    Returns:
        初始化后的 AppContainer 实例
    """
    global _container

    _container = AppContainer(
        # 客户端
        ha_service=services["ha_service"],
        llm_chat_client=services["llm_chat_client"],
        vision_client=services["vision_client"],
        summary_client=services["summary_client"],
        embed_client=services["embed_client"],
        # 服务
        session_store=services["session_store"],
        vision_service=services["vision_service"],
        vision_key_pool=services["vision_key_pool"],
        rule_service=services["rule_service"],
        rule_registry_service=services["rule_registry_service"],
        automation_service=services["automation_service"],
        summarization_service=services["summarization_service"],
        llm_settings_service=services["llm_settings_service"],
        emoji_service=services["emoji_service"],
        metrics_service=metrics_service,
        # MCP / 工具
        mcp_client_manager=services["mcp_client_manager"],
        tool_executor=services["tool_executor"],
        # 摄像头
        camera_stream=services["camera_stream"],
        camera_manager=services.get("camera_manager"),   # Task 7:可能未注入(过渡)
        discovery_service=services.get("discovery_service"),  # Task 6:ONVIF 发现
        ptz_registry=services.get("ptz_registry"),  # Task 5:PtzRegistry
        # 语义图
        sg_service=services["sg_service"],
        # 集成插件平台（lifespan 阶段赋值，此处占位 None）
        integration_layer=services.get("integration_layer"),
        # 可变引用
        ha_client_ref=services["ha_client_ref"],
        automation_agent_ref=services["automation_agent_ref"],
        ha_catalog_cache_ref=services["ha_catalog_cache_ref"],
        ha_controls_cache_ref=[""],  # 新建，之前是 main.py 模块级变量
    )

    logger.info("AppContainer initialized with %d services", len(services))
    return _container
