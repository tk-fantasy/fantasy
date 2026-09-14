"""用户管理相关路由。"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request, Response

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user, create_access_token, create_refresh_token, is_secure_request, set_auth_cookies, verify_password
from ..core.database import Database
from ..core.exceptions import AppException
from ..schema.api_schemas import UserLLMKeysRequest, UserSwitchRequest

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


@router.post("/users/{username}/llm_keys")
async def save_user_llm_keys(
    username: str,
    payload: UserLLMKeysRequest,
    current_user: dict = Depends(get_current_user),
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

    # 保存到 DB（含全部角色；vision/embed 也写 per-user DB 作为全局 key 备份，
    # 供 main.py 启动自愈在全局 .env 丢失时恢复——"将错就错"容错策略）
    await db.user_setting_set(user["id"], "llm_keys", json.dumps(keys, ensure_ascii=False))

    # 只写 per-user DB，不动全局：per-user 解析在请求时实时读 DB，后台任务
    # （自动化兜底/周报/摘要/RAG）继续用启动时加载的全局 key——与 switch_user
    # 的约定一致。此处覆盖全局 CONFIG/.env/全局客户端的话，多用户下"最后保存
    # 的人"会劫持所有走全局解析的后台任务。
    return ApiResponse(data={"saved": True, "count": len(keys)})


