"""LLM Key 查找与解析 — 统一管理 config.json 中的 llm_keys。

所有 key 查找逻辑集中在此模块，避免 5+ 个文件各自实现。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from .config import get_config

logger = logging.getLogger(__name__)


def find_key_by_id(key_id: str | None) -> dict[str, Any] | None:
    """从 llm_keys 中查找指定 ID 的 key。

    Args:
        key_id: key 的唯一标识符

    Returns:
        key 配置字典，未找到返回 None
    """
    if not key_id:
        return None
    raw_keys = get_config("llm_keys", [])
    for item in raw_keys:
        if item.get("id") == key_id:
            return item
    return None


def auto_select_key(role: str) -> dict[str, Any] | None:
    """自动选择第一个 type 匹配且有 API key 的条目。

    当 providers.<role>.key_id 未设置或找不到时调用。

    Args:
        role: 角色类型（chat/vision/embed/summary）

    Returns:
        key 配置字典，无可用 key 返回 None
    """
    raw_keys = get_config("llm_keys", [])
    for item in raw_keys:
        if item.get("type") != role:
            continue
        api_key = resolve_api_key(item)
        if api_key:
            return item
    return None


def get_keys_for_role(role: str) -> list[dict[str, Any]]:
    """获取指定角色的所有可用 key（已解析 API key）。

    用于 ApiKeyManager 构建 key 池。

    Args:
        role: 角色类型（chat/vision/embed/summary）

    Returns:
        key 配置列表，每项包含解析后的 api_key
    """
    raw_keys = get_config("llm_keys", [])
    entries: list[dict[str, Any]] = []
    for item in raw_keys:
        if item.get("type") != role:
            continue
        api_key = resolve_api_key(item)
        if not api_key:
            continue
        entries.append({
            "id": item.get("id", ""),
            "api_key": api_key,
            "model": item.get("model", ""),
            "base_url": item.get("base_url", "").rstrip("/"),
            "chat_path": item.get("chat_path", "/chat/completions"),
            "embed_path": item.get("embed_path", "/v1/embeddings"),
        })
    return entries


def resolve_api_key(key_entry: dict[str, Any]) -> str:
    """从 key 配置中解析 API key。

    优先从环境变量读取（api_key_env），其次读直接值（api_key）。

    Args:
        key_entry: key 配置字典

    Returns:
        API key 字符串，未设置返回空字符串
    """
    env_name = key_entry.get("api_key_env", "")
    if env_name:
        return os.getenv(env_name, "")
    return key_entry.get("api_key", "")


def resolve_key_for_role(role: str) -> dict[str, Any] | None:
    """为指定角色解析完整的 key 配置。

    优先使用 providers.<role>.key_id 指定的 key，
    找不到则自动选择第一个可用的 key。

    Args:
        role: 角色类型（chat/vision/embed/summary）

    Returns:
        key 配置字典（含解析后的 api_key），无可用 key 返回 None
    """
    # 1. 尝试按 key_id 查找
    key_id = get_config(f"providers.{role}.key_id")
    key_entry = find_key_by_id(key_id)

    # 2. 回退到自动选择
    if not key_entry:
        key_entry = auto_select_key(role)
        if key_entry:
            logger.info("Auto-selected %s key: %s (%s)", role, key_entry.get("id"), key_entry.get("model"))

    if not key_entry:
        return None

    # 3. 解析 API key 并返回
    result = dict(key_entry)
    result["api_key"] = resolve_api_key(key_entry)
    return result


async def resolve_key_for_role_user(role: str, user_id: str) -> dict[str, Any] | None:
    """按 user_id 从 DB 解析指定角色的 key（含明文 api_key）。

    读取该用户的 user_settings.llm_keys + user_settings.providers，
    解析出 base_url/model/api_key。用户无配置时返回 None（调用方回退全局）。

    Args:
        role: 角色类型（chat/vision/embed/summary/stt）
        user_id: 用户 ID

    Returns:
        key 配置字典（含解析后的 api_key），无可用 key 返回 None
    """
    if not user_id:
        return None

    import json
    from .database import Database

    db = Database.get()
    llm_keys_json = await db.user_setting_get(user_id, "llm_keys")
    providers_json = await db.user_setting_get(user_id, "providers")
    if not llm_keys_json:
        return None

    try:
        llm_keys = json.loads(llm_keys_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not llm_keys:
        return None

    try:
        providers = json.loads(providers_json) if providers_json else {}
    except (json.JSONDecodeError, TypeError):
        providers = {}

    # 0. use_global flag：chat/summary/stt 角色可显式声明"用全局 key"，
    # 此处返回 None 让调用方回退到 resolve_key_for_role(全局)。
    # 必须在 key_id 查找与 auto-select 之前判断——否则 auto-select 会拦下
    # 用户已有的同类型 per-user key，导致"切到全局"无效。
    if isinstance(providers, dict):
        role_provider = providers.get(role) or {}
        if isinstance(role_provider, dict) and role_provider.get("use_global"):
            return None

    # 1. 按 providers 绑定查找
    key_id = providers.get(role, {}).get("key_id") if isinstance(providers, dict) else None
    key_entry = None
    if key_id:
        key_entry = next((k for k in llm_keys if k.get("id") == key_id), None)

    # 2. 回退到自动选择（第一个 type 匹配且有 key 的）
    if not key_entry:
        for item in llm_keys:
            if item.get("type") != role:
                continue
            # per-user 的 key 存了明文 api_key 字段
            if item.get("api_key") or os.getenv(item.get("api_key_env", "")):
                key_entry = item
                break

    if not key_entry:
        return None

    # 3. 解析明文 key：优先 api_key 字段，回退 env
    api_key = key_entry.get("api_key", "")
    if not api_key:
        env_name = key_entry.get("api_key_env", "")
        if env_name:
            api_key = os.getenv(env_name, "")

    if not api_key:
        return None

    return {
        "id": key_entry.get("id", ""),
        "base_url": key_entry.get("base_url", "").rstrip("/"),
        "model": key_entry.get("model", ""),
        "type": key_entry.get("type", ""),
        "api_key": api_key,
        "chat_path": key_entry.get("chat_path", "/chat/completions"),
        "embed_path": key_entry.get("embed_path", "/v1/embeddings"),
    }
