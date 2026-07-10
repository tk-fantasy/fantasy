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
