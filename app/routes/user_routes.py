"""用户管理相关路由。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, Response

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user, create_access_token, create_refresh_token, is_secure_request, set_auth_cookies, verify_password
from ..core.config import get_config, update_memory_config, write_secrets
from ..core.database import Database
from ..core.exceptions import AppException
from ..core.roles import PER_USER_ROLES
from ..schema.api_schemas import UserLLMKeysRequest, UserProvidersRequest, UserSwitchRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["users"])


@router.get("/users")
async def list_users(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    """获取所有已完成初始配置的用户列表（有 LLM keys 的用户）。"""
    db = Database.get()
    all_users = await db.user_list_all()

    # 过滤出已完成配置的用户（有 LLM keys）
    configured_users = []
    for user in all_users:
        llm_keys_json = await db.user_setting_get(user["id"], "llm_keys")
        if llm_keys_json:
            keys = json.loads(llm_keys_json)
            if keys:  # 有 LLM keys 的用户
                configured_users.append(user)

    return ApiResponse(data=configured_users)


@router.get("/users/me")
async def get_current_user_info(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """获取当前用户信息。"""
    db = Database.get()
    user = await db.user_get_by_id(current_user["user_id"])
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)
    return ApiResponse(data=user)


@router.post("/users/switch")
async def switch_user(
    request: Request,
    payload: UserSwitchRequest,
    response: Response,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """切换到指定用户。

    per-user key 现在从 DB 按用户直接读取，不再覆盖全局 CONFIG / .env / 全局 LLM 客户端。
    全局 CONFIG 保留启动时加载的默认用户 key（供后台任务用）。
    """
    username = payload.username.strip()
    password = payload.password

    db = Database.get()

    # 查找目标用户
    target_user = await db.user_get_by_username(username)
    if not target_user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)

    # 校验目标用户密码，防止冒充
    if not verify_password(password, target_user["password_hash"]):
        raise AppException("密码错误", code="invalid_credentials", http_status=401)

    # 设置新用户的 cookie
    access_token = create_access_token(target_user["id"], target_user["username"])
    refresh_token = create_refresh_token(target_user["id"])
    set_auth_cookies(response, access_token, refresh_token, secure=is_secure_request(request))

    logger.info("User switched to: %s (%s)", username, target_user["id"])

    return ApiResponse(data={
        "switched_to": username,
        "user": {
            "id": target_user["id"],
            "username": target_user["username"],
            "display_name": target_user.get("display_name", target_user["username"]),
        },
    })


@router.get("/users/{username}/llm_keys")
async def get_user_llm_keys(
    username: str,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    """获取指定用户的 LLM keys。"""
    db = Database.get()
    user = await db.user_get_by_username(username)
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)

    llm_keys_json = await db.user_setting_get(user["id"], "llm_keys")
    llm_keys = json.loads(llm_keys_json) if llm_keys_json else []

    # 返回时隐藏实际 API key
    result = []
    for key in llm_keys:
        result.append({
            "id": key.get("id"),
            "base_url": key.get("base_url", ""),
            "model": key.get("model", ""),
            "type": key.get("type", ""),
            "chat_path": key.get("chat_path", "/chat/completions"),
            "embed_path": key.get("embed_path", ""),
            "api_key_env": key.get("api_key_env", ""),
            "api_key_set": bool(key.get("api_key", "")),
        })

    return ApiResponse(data=result)


@router.post("/users/{username}/llm_keys")
async def save_user_llm_keys(
    username: str,
    payload: UserLLMKeysRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """保存用户的 LLM keys。仅允许修改自己的配置。"""
    db = Database.get()
    user = await db.user_get_by_username(username)
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)

    # 仅允许修改自己的配置
    if user["id"] != current_user["user_id"]:
        raise AppException("无权修改他人配置", code="forbidden", http_status=403)

    keys = payload.keys

    # per-user DB 仅存 PER_USER_ROLES（chat/summary/stt）；
    # vision/embed 全局共享，不进用户 DB（否则全局 .env 丢失后运行时
    # 无法回退，RAG/语义图/emoji 全部 401）。update_memory_config /
    # write_secrets 仍用完整 keys，保持全局 CONFIG 的 embed/vision 不被冲掉。
    per_user_keys = [k for k in keys if k.get("type") in PER_USER_ROLES]

    # 保存到 DB（仅 per-user 角色）
    await db.user_setting_set(user["id"], "llm_keys", json.dumps(per_user_keys, ensure_ascii=False))

    # 如果是当前用户，同时更新内存配置（不写 config.json）—— 用完整 keys
    if user["id"] == current_user["user_id"]:
        update_memory_config("llm_keys", keys)

        # 更新 .env
        env_updates = {}
        for key in keys:
            env_name = key.get("api_key_env", "")
            api_key = key.get("api_key", "")
            if env_name and api_key:
                env_updates[env_name] = api_key
        if env_updates:
            write_secrets(env_updates)

        # 重载客户端
        try:
            container.reload_all_clients()
        except Exception as e:
            logger.warning("Failed to reload LLM clients: %s", e)

    return ApiResponse(data={"saved": True, "count": len(per_user_keys)})


@router.get("/users/{username}/providers")
async def get_user_providers(
    username: str,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """获取指定用户的 providers 配置。"""
    db = Database.get()
    user = await db.user_get_by_username(username)
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)

    providers_json = await db.user_setting_get(user["id"], "providers")
    providers = json.loads(providers_json) if providers_json else {}

    return ApiResponse(data=providers)


@router.post("/users/{username}/providers")
async def save_user_providers(
    username: str,
    payload: UserProvidersRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """保存用户的 providers 配置。仅允许修改自己的配置。"""
    db = Database.get()
    user = await db.user_get_by_username(username)
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)

    # 仅允许修改自己的配置
    if user["id"] != current_user["user_id"]:
        raise AppException("无权修改他人配置", code="forbidden", http_status=403)

    providers = payload.providers

    # 保存到 DB
    await db.user_setting_set(user["id"], "providers", json.dumps(providers, ensure_ascii=False))

    # 如果是当前用户，同时更新内存配置（不写 config.json）
    if user["id"] == current_user["user_id"]:
        update_memory_config("providers", providers)

    return ApiResponse(data={"saved": True})
