"""启动自愈：全局 LLM key 无效时从 per-user DB 恢复。

独立模块，避免 main.py 顶部 import faiss 导致测试环境无法导入。
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


def is_api_key_valid(api_key: str) -> bool:
    """判断 api_key 是否有效：非空且非占位符。

    占位符模式：your_*_here / your-*（wizard 模板/示例值）。
    """
    if not api_key:
        return False
    k = api_key.strip().lower()
    if not k:
        return False
    if k.startswith("your_") or k.startswith("your-"):
        return False
    return True


async def heal_global_keys_from_user_db() -> dict[str, str]:
    """启动自愈：全局 llm_keys 中无效的角色 key，从 per-user DB 恢复。

    遍历全局 llm_keys 的每个角色，若 resolve_api_key 解析为空或占位符，
    则扫所有用户的 user_settings.llm_keys，找第一个有该角色**有效明文
    api_key**的条目，把明文 key 写回 .env（对应 api_key_env 变量）+
    更新内存 CONFIG 的 key 条目（补 api_key 字段）。

    典型场景：wizard 把 embed/vision key 同时写进全局 .env（env 引用）
    和 per-user DB（明文）。容器重建后 .env 丢失/占位符 → 全局解析为空，
    但 per-user DB 的明文 key 还在，此处一次性恢复。

    Returns:
        role -> api_key 的映射，表示已恢复的角色。空 dict 表示无需恢复
        或 per-user DB 也无可用 key。调用方可据此决定是否 reload clients。
    """
    from .config import get_config, update_memory_config, write_secrets
    from .database import Database
    from .key_resolver import resolve_api_key

    global_keys = get_config("llm_keys", []) or []
    if not global_keys:
        return {}  # 空 config 已由 main.py 的迁移块处理

    # 找出无效角色（全局解析为空/占位符）
    invalid_roles: set[str] = set()
    for k in global_keys:
        role = k.get("type", "")
        if role and role not in invalid_roles:
            if not is_api_key_valid(resolve_api_key(k)):
                invalid_roles.add(role)
    if not invalid_roles:
        return {}  # 全部角色有效，无需自愈

    logger.info("Global LLM keys invalid for roles: %s — healing from user DB", sorted(invalid_roles))

    db = Database.get()
    all_users = await db.user_list_all()
    # role -> 明文 api_key（从第一个有该角色有效 key 的用户取）
    healed: dict[str, str] = {}
    for user in all_users:
        if len(healed) == len(invalid_roles):
            break
        llm_keys_json = await db.user_setting_get(user["id"], "llm_keys")
        if not llm_keys_json:
            continue
        try:
            user_keys = json.loads(llm_keys_json)
        except (json.JSONDecodeError, TypeError):
            continue
        for k in user_keys:
            role = k.get("type", "")
            if role not in invalid_roles or role in healed:
                continue
            # per-user DB 存明文 api_key 字段
            api_key = k.get("api_key", "")
            if not api_key:
                # 回退读 env
                env_name = k.get("api_key_env", "")
                if env_name:
                    api_key = os.getenv(env_name, "")
            if is_api_key_valid(api_key):
                healed[role] = api_key

    if not healed:
        logger.info("No valid keys found in user DB for invalid roles: %s", sorted(invalid_roles))
        return {}

    # 写回：更新内存 CONFIG 的 key 条目（补 api_key 字段）+ .env
    env_updates: dict[str, str] = {}
    for k in global_keys:
        role = k.get("type", "")
        if role not in healed:
            continue
        api_key = healed[role]
        k["api_key"] = api_key  # 内存 CONFIG 直接补明文（resolve_api_key 会优先读 api_key 字段）
        env_name = k.get("api_key_env", "")
        if env_name:
            env_updates[env_name] = api_key

    update_memory_config("llm_keys", global_keys)
    if env_updates:
        write_secrets(env_updates)
        # 同步进程内 env，让本轮启动后续的 resolve_api_key 能读到
        for name, val in env_updates.items():
            os.environ[name] = val

    logger.info(
        "Healed %d global LLM keys from user DB: %s",
        len(healed), sorted(healed.keys()),
    )
    return healed
