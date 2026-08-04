"""per-user LLM 客户端工厂 —— 统一"按 user_id 解析 key → 构造独立客户端"逻辑。

原先 automation / rule / scheduler / summarization 四处各自复制了同一段：
    resolve_key_for_role_user → LlmChatClient(role=...) → 覆盖 _api_key/_base_url/_model
差异只有两点：role（chat vs summary）与是否强制 _enabled=True。本工厂参数化这两个差异，
fallback（无 per-user 配置时回退到哪个全局客户端）留给各调用方——因为它们回退的对象不同
（有的回退注入实例，有的 lazy-init），强行统一会改变边界行为。
"""
from __future__ import annotations

import logging

from .llm_chat_client import LlmChatClient

logger = logging.getLogger(__name__)


async def build_per_user_chat_client(
    role: str,
    user_id: str,
    force_enabled: bool = True,
) -> LlmChatClient | None:
    """按 user_id 解析 per-user key 并构造独立 LlmChatClient。

    Args:
        role: 客户端角色（chat / summary）。automation/rule/scheduler 用 chat，
              summarization 用 summary。
        user_id: 用户 ID，空串直接返回 None（走调用方的全局回退）。
        force_enabled: 是否强制 _enabled=True。per-user 有 key 即启用、不受全局
            llm.enabled 开关影响（automation/rule/scheduler 需要此行为）；
            summarization 传 False，保留 summary 角色的 enabled 开关语义。

    Returns: 构造好的 LlmChatClient；user_id 为空或无 per-user 配置返回 None，
             由调用方各自回退到全局客户端。
    """
    if not user_id:
        return None
    try:
        from ..core.key_resolver import resolve_key_for_role_user
        key_info = await resolve_key_for_role_user(role, user_id)
        if not key_info or not key_info.get("api_key"):
            return None
        client = LlmChatClient(role=role)
        client._api_key = key_info["api_key"]
        client._base_url = key_info["base_url"]
        client._model = key_info["model"]
        if force_enabled:
            client._enabled = True
        return client
    except Exception:
        logger.debug(
            "Failed to resolve per-user %s client, caller will fall back to global",
            role, exc_info=True,
        )
        return None
