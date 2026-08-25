"""Per-user LLM Key / 模型设置路由。

从 settings_routes.py 拆出。端点路径不变（/api 前缀由 main.py include 时加）。
业务逻辑（key 池重载、用户配置同步）下沉到 llm_key_service。
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from urllib.parse import urlparse

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.config import delete_llm_key, get_config, upsert_llm_key
from ..core.database import Database
from ..core.exceptions import AppException
from ..core.key_resolver import resolve_key_for_role, resolve_key_for_role_user
from ..core.roles import PER_USER_ROLES
from ..schema.api_schemas import LLMKeyRequest, LLMSettingsRequest
from ..services import llm_key_service
from ..services.model_test_service import test_model_connection

logger = logging.getLogger(__name__)

router = APIRouter()

# /chat 状态条展示的 4 个角色（stt 语音专用，不在此列）
_LLM_STATUS_ROLES: list[str] = ["chat", "summary", "vision", "embed"]


@router.get("/llm_keys")
async def list_llm_keys(current_user: dict = Depends(get_current_user)) -> ApiResponse[list[dict]]:
    """列出当前用户的 LLM Keys（不含密钥值）。"""
    db = Database.get()
    llm_keys_json = await db.user_setting_get(current_user["user_id"], "llm_keys")
    if not llm_keys_json:
        return ApiResponse(data=[])
    keys = json.loads(llm_keys_json)
    import os
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
            "api_key_set": bool(os.getenv(env_name)) if env_name else bool(k.get("api_key", "")),
        })
    return ApiResponse(data=out)


@router.post("/llm_keys")
async def upsert_llm_key_route(
    payload: LLMKeyRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    """添加或更新 LLM Key。新增时自动测试连接。"""
    base_url = payload.base_url.strip()
    model = payload.model.strip()
    model_type = payload.type.strip()
    api_key = payload.api_key.strip()
    key_id = payload.id.strip()

    if model_type not in ("chat", "summary", "vision", "embed", "stt"):
        raise AppException("type 必须是 chat/summary/vision/embed/stt 之一", code="llm_key_invalid", http_status=400)

    parsed = urlparse(base_url)
    is_local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if is_local and not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    if model_type == "embed":
        chat_path, embed_path = "", "/embeddings"
    elif model_type == "stt":
        chat_path, embed_path = "", ""
    else:
        chat_path, embed_path = "/chat/completions", ""

    is_new = not key_id
    if is_new:
        key_id = llm_key_service.generate_key_id(base_url)

    if is_new and api_key:
        test_result = await test_model_connection(
            base_url=base_url, model=model, role=model_type,
            api_key=api_key, embed_path=embed_path,
        )
        if not test_result.get("ok"):
            raise AppException(
                f"连接测试失败: {test_result.get('error', '未知错误')}",
                code="llm_key_test_failed", http_status=400,
            )

    entry = {
        "id": key_id, "base_url": base_url, "model": model, "type": model_type,
        "chat_path": chat_path, "embed_path": embed_path,
    }
    keys = upsert_llm_key(entry, api_key_value=api_key if api_key else None)
    llm_key_service.reload_key_pools(container)
    await llm_key_service.sync_llm_keys_to_current_user(current_user)
    return ApiResponse(data=keys)


@router.delete("/llm_keys/{key_id}")
async def delete_llm_key_route(
    key_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    """删除 LLM Key。"""
    keys = delete_llm_key(key_id)
    llm_key_service.reload_key_pools(container)
    await llm_key_service.sync_llm_keys_to_current_user(current_user)
    return ApiResponse(data=keys)


@router.get("/llm/settings")
async def get_llm_settings(
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """获取当前 LLM 设置。chat/summary/stt 返回 per-user 绑定；vision/embed 返回全局。"""
    settings = container.llm_settings_service.current_settings()
    user_providers = await llm_key_service.get_user_providers(current_user["user_id"])
    for role in PER_USER_ROLES:
        if role in user_providers:
            merged = {
                "key_id": None,
                "max_concurrency": int(get_config(f"providers.{role}.max_concurrency", 8)),
                "thinking": False,
                "use_global": False,
            }
            merged.update(user_providers[role])
            settings[role] = merged
        else:
            settings[role] = {
                "key_id": None,
                "max_concurrency": int(get_config(f"providers.{role}.max_concurrency", 8)),
                "thinking": False,
                "use_global": False,
            }
    return ApiResponse(data={
        "current": settings,
        "warnings": container.llm_settings_service.warnings(),
    })


@router.post("/llm/settings")
async def set_llm_settings(
    payload: LLMSettingsRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """应用 LLM 设置。vision/embed 写全局 config.json；chat/summary/stt 写用户 DB。"""
    role = payload.role

    if role in PER_USER_ROLES:
        values: dict = {
            "key_id": payload.key_id,
            "max_concurrency": max(1, payload.max_concurrency or 8),
            "enabled": True,
        }
        if role in ("chat", "summary") and payload.thinking is not None:
            values["thinking"] = bool(payload.thinking)
        if payload.use_global is not None:
            values["use_global"] = bool(payload.use_global)
            if payload.use_global:
                values["key_id"] = ""
        await llm_key_service.save_user_provider(
            current_user["user_id"], role, values.get("key_id", payload.key_id), values,
        )
        if hasattr(container.dispatcher, "invalidate_user_agent"):
            await container.dispatcher.invalidate_user_agent(current_user["user_id"])
        logger.info("Per-user provider saved", extra={
            "role": role, "user_id": current_user["user_id"],
            "use_global": values.get("use_global"),
        })
        return ApiResponse(data={"role": role, "applied": values})

    result = container.llm_settings_service.apply(
        role=role, key_id=payload.key_id,
        max_concurrency=payload.max_concurrency,
        thinking=payload.thinking, multimodal=payload.multimodal,
    )
    try:
        db = Database.get()
        providers = get_config("providers", {})
        await db.user_setting_set(
            current_user["user_id"], "providers",
            json.dumps(providers, ensure_ascii=False),
        )
    except Exception as e:
        logger.warning(f"Failed to sync providers to user: {e}")
    return ApiResponse(data=result)


@router.get("/llm/status")
async def get_llm_status(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """各 LLM 角色实际生效的模型配置 + 连通性测试。"""
    user_id = current_user["user_id"]
    results: dict[str, dict] = {}

    for role in _LLM_STATUS_ROLES:
        entry: dict = {
            "model": "", "base_url": "", "source": "global",
            "connected": False, "error": None,
        }
        key_info = None
        if role in PER_USER_ROLES:
            key_info = await resolve_key_for_role_user(role, user_id)
            if key_info:
                entry["source"] = "user"
        if not key_info:
            key_info = resolve_key_for_role(role)
            entry["source"] = "global"

        if not key_info or not key_info.get("api_key"):
            entry["error"] = "未配置可用的 API Key"
            results[role] = entry
            continue

        entry["model"] = key_info.get("model", "")
        entry["base_url"] = key_info.get("base_url", "")

        test = await test_model_connection(
            base_url=key_info["base_url"], model=key_info["model"], role=role,
            api_key=key_info["api_key"],
            chat_path=key_info.get("chat_path", "/chat/completions"),
            embed_path=key_info.get("embed_path", "/embeddings"),
        )
        entry["connected"] = bool(test.get("ok"))
        if not entry["connected"]:
            entry["error"] = test.get("error", "未知错误")
        results[role] = entry

    return ApiResponse(data={"roles": results})
