"""服务初始化和启动逻辑。"""
from __future__ import annotations

import logging
from typing import Any

from .camera_stream import CameraStream
from .services.camera_manager import CameraManager
from .clients.ha_client import HomeAssistantClient
from .clients.llm_chat_client import LlmChatClient
from .clients.llm_vision_client import LlmVisionClient
from .core.config import get_config
from .mcp.mcp_client_manager import MCPClientManager
from .mcp.tool_executor import ToolExecutor
from .services.api_key_manager import ApiKeyManager
from .services.automation_service import AutomationService
from .services.camera_discovery_service import discovery_service
from .services.emoji_service import EmojiService
from .services.ha_service import HAService
from .services.llm_settings_service import LlmSettingsService
from .services.rule_registry_service import RuleRegistryService
from .services.rule_service import RuleService
from .services.session_store import SessionStore
from .services.summarization_service import SummarizationService
from .services.vision_service import VisionService

logger = logging.getLogger(__name__)


def initialize_services() -> dict[str, Any]:
    """初始化所有服务并返回服务字典。"""
    services: dict[str, Any] = {}

    # 视觉客户端和 key pool
    vision_client = LlmVisionClient()
    vision_key_pool = ApiKeyManager(role="vision")
    vision_client.set_key_pool(vision_key_pool)
    vision_service = VisionService(client=vision_client)

    services["vision_client"] = vision_client
    services["vision_key_pool"] = vision_key_pool
    services["vision_service"] = vision_service

    # 摄像头流（camera_index 由 CameraStream 从 vision.camera_index 读取，默认 0）
    camera_stream = CameraStream(vision_service=vision_service)
    services["camera_stream"] = camera_stream

    # Task 7:多路 CameraManager。db/ha/automation 后注入(bootstrap 顺序兜底)。
    camera_manager = CameraManager(
        vision_service=vision_service,
        discovery_service=discovery_service,
    )
    services["camera_manager"] = camera_manager

    # 聊天客户端（用于摘要等后台任务）
    llm_chat_client = LlmChatClient()
    summary_client = LlmChatClient(role="summary")

    services["llm_chat_client"] = llm_chat_client
    services["summary_client"] = summary_client

    # 会话存储
    session_store = SessionStore(get_config("storage.session_file") or None)
    services["session_store"] = session_store

    # MCP 工具管理
    # 工具注册统一在 tools.register_all_tools（lifespan 阶段）完成，
    # 此处只构造 manager + executor，不提前注册工具。
    mcp_client_manager = MCPClientManager()
    tool_executor = ToolExecutor(mcp_client_manager)

    services["mcp_client_manager"] = mcp_client_manager
    services["tool_executor"] = tool_executor

    # LangGraph Agent 和工具列表在 main.py lifespan 中构建（所有工具注册完毕后）
    services["langgraph_agent"] = None
    services["langchain_tools"] = None

    # RAG 相关服务
    summarization_service = SummarizationService(chat_client=summary_client)

    services["summarization_service"] = summarization_service

    # 规则相关服务
    rule_service = RuleService(llm_chat_client)
    rule_registry_service = RuleRegistryService()

    services["rule_service"] = rule_service
    services["rule_registry_service"] = rule_registry_service

    # Home Assistant 客户端（自动化服务的设备状态门控依赖 ha_service）
    ha_client = HomeAssistantClient()
    ha_service = HAService(client=ha_client)
    services["ha_client"] = ha_client
    services["ha_service"] = ha_service

    # 自动化服务
    automation_service = AutomationService(
        rule_registry_service,
        tool_executor=tool_executor,
        vision_service=vision_service,
        ha_service=ha_service,
    )
    services["automation_service"] = automation_service

    # LLM 设置服务
    llm_settings_service = LlmSettingsService()

    # 注册热重载钩子
    llm_settings_service.register_reload_hook(llm_chat_client.reload)
    llm_settings_service.register_reload_hook(summary_client.reload)
    llm_settings_service.register_reload_hook(vision_client.reload)

    services["llm_settings_service"] = llm_settings_service

    # Emoji 搜索服务（embed 客户端 + 向量索引）
    embed_client = LlmChatClient(role="embed")
    emoji_service = EmojiService(embed_client=embed_client)

    services["embed_client"] = embed_client
    services["emoji_service"] = emoji_service

    # embed_client 的热重载钩子须在实例创建后注册
    llm_settings_service.register_reload_hook(embed_client.reload)

    # 语义图构建服务（复用 embed_client + llm_chat_client）
    from .services.sg_service import SemanticGraphService
    sg_service = SemanticGraphService(embed_client=embed_client, llm_chat_client=llm_chat_client)
    services["sg_service"] = sg_service

    # 自动化评估 agent 引用（lifespan 中创建）
    services["automation_agent_ref"] = [None]

    # HA 设备目录缓存引用
    services["ha_catalog_cache_ref"] = [""]

    # HA 客户端引用（支持热重建）
    services["ha_client_ref"] = [ha_client]

    # ONVIF 摄像头自动发现（无状态,被动调用）
    services["discovery_service"] = discovery_service

    return services
