"""LLM Key / 全局配置的业务逻辑（从 settings_routes 下沉）。

路由层只做参数校验 + 调本 service + 返回响应；
key 池重载、agent 热重建、用户配置同步、二级密码校验等业务逻辑集中于此。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import uuid
from urllib.parse import urlparse

from ..container import AppContainer
from ..core.auth import verify_password
from ..core.config import get_config, get_secondary_password_hash
from ..core.database import Database
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)


def is_secondary_password_set() -> bool:
    """是否已设置二级密码（路由层判断用，统一走 service 便于测试 patch）。"""
    return bool(get_secondary_password_hash())


def reload_key_pools(container: AppContainer) -> None:
    """llm_keys 改动后重建 key 池，并检测 embed 模型变更触发 RAG 重建。"""
    container.vision_key_pool.reload()
    container.embed_client.reload()
    if container.rag_service:
        container.rag_service.maybe_rebuild_if_model_changed()


async def sync_llm_keys_to_current_user(current_user: dict) -> None:
    """同步当前 config 的 llm_keys 到指定用户的 user_settings。

    写入 DB 时把实际 api_key 也存入 api_key 字段（从 env 读取），
    供 per-user 解析使用。明文 key 仅存于本地 SQLite，不会返回前端。

    注：vision/embed 也会同步进 per-user DB 作为"全局 key 备份"——
    全局 .env 丢失时，启动自愈逻辑（main.py）会从 per-user DB 恢复
    全局 key。这是"将错就错"的容错策略，不再强制过滤。
    """
    try:
        # 局部 import 便于测试 patch app.core.config.get_config
        from ..core.config import get_config as _get_config
        db = Database.get()
        keys = copy.deepcopy(_get_config("llm_keys", []))
        for k in keys:
            env_name = k.get("api_key_env", "")
            if env_name and not k.get("api_key"):
                k["api_key"] = os.getenv(env_name, "")
        await db.user_setting_set(
            current_user["user_id"],
            "llm_keys",
            json.dumps(keys, ensure_ascii=False),
        )
    except Exception as e:
        logger.warning(f"Failed to sync llm_keys to user: {e}")


async def save_user_provider(user_id: str, role: str, key_id: str, values: dict) -> None:
    """把 per-user provider 绑定写入用户 DB。"""
    try:
        db = Database.get()
        providers_json = await db.user_setting_get(user_id, "providers")
        providers = json.loads(providers_json) if providers_json else {}
        providers[role] = {**values, "key_id": key_id}
        await db.user_setting_set(
            user_id, "providers",
            json.dumps(providers, ensure_ascii=False),
        )
    except Exception as e:
        logger.warning(f"Failed to save user provider: {e}")


async def get_user_providers(user_id: str) -> dict:
    """读取用户 DB 中的 providers 绑定。"""
    try:
        db = Database.get()
        providers_json = await db.user_setting_get(user_id, "providers")
        return json.loads(providers_json) if providers_json else {}
    except Exception:
        return {}


def generate_key_id(base_url: str) -> str:
    """从 base_url 生成 key ID。"""
    parsed = urlparse(base_url)
    host = parsed.hostname or "unknown"
    parts = host.split(".")
    prefix = parts[0] if parts else host
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}-{suffix}"


def verify_secondary_password(password: str) -> None:
    """验证二级密码，失败抛 403。未设置密码时拒绝（要求先走首次设置流程）。"""
    stored = get_secondary_password_hash()
    if not stored:
        raise AppException(
            "尚未设置全局配置二级密码，请先完成首次设置",
            code="secondary_password_not_set",
            http_status=403,
        )
    if not password or not verify_password(password, stored):
        raise AppException(
            "二级密码错误",
            code="secondary_password_invalid",
            http_status=403,
        )


def mask_global_keys(keys: list[dict]) -> list[dict]:
    """返回全局 key 列表给前端（隐藏明文 api_key，保留 api_key_env + 是否已配置标记）。"""
    out = []
    for k in keys:
        env_name = k.get("api_key_env", "")
        out.append({
            "id": k.get("id"),
            "base_url": k.get("base_url", ""),
            "model": k.get("model", ""),
            "type": k.get("type", ""),
            "chat_path": k.get("chat_path", "/chat/completions"),
            "embed_path": k.get("embed_path", "/v1/embeddings"),
            "api_key_env": env_name,
            "api_key_set": bool(os.getenv(env_name)) if env_name else False,
        })
    return out
