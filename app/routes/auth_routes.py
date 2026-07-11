"""认证相关 API 路由。"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response

from ..core.api_models import ApiResponse
from ..core.auth import (
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    extract_refresh_token_from_request,
    get_current_user,
    hash_password,
    is_secure_request,
    set_auth_cookies,
    verify_password,
    verify_token,
)
from ..core.database import Database
from ..core.exceptions import AppException
from ..core.rate_limit import RateLimiter
from ..schema.api_schemas import AuthLoginRequest, AuthRegisterRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# Rate limiters for auth endpoints
_login_limiter = RateLimiter(max_requests=5, window_seconds=60)  # 5 attempts per minute
_register_limiter = RateLimiter(max_requests=3, window_seconds=60)  # 3 attempts per minute


@router.post("/auth/register")
async def register(request: Request, response: Response, payload: AuthRegisterRequest) -> ApiResponse[dict]:
    """用户注册。

    第一个注册的用户自动成为管理员（预留字段，暂未使用）。
    """
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not _register_limiter.check(client_ip):
        raise AppException("注册请求过于频繁，请稍后再试", code="rate_limit_exceeded", http_status=429)

    username = payload.username.strip()
    password = payload.password
    display_name = username  # Pydantic model doesn't have display_name, use username

    db = Database.get()

    # 检查用户名是否已存在
    existing = await db.user_get_by_username(username)
    if existing:
        raise AppException("用户名已存在", code="username_exists", http_status=400)

    # 创建用户
    user_id = str(uuid.uuid4())
    password_hash = hash_password(password)
    user = await db.user_create(user_id, username, password_hash, display_name)

    # 初始化新用户的 user_settings（空的 llm_keys 和 providers）
    import json
    await db.user_setting_set(user_id, "llm_keys", json.dumps([], ensure_ascii=False))
    await db.user_setting_set(user_id, "providers", json.dumps({}, ensure_ascii=False))
    logger.info("Initialized user_settings for new user: %s", username)

    # 自动生成 token 并设置 cookie
    access_token = create_access_token(user_id, username)
    refresh_token = create_refresh_token(user_id)
    set_auth_cookies(response, access_token, refresh_token, secure=is_secure_request(request))

    logger.info("User registered: %s (%s)", username, user_id)

    return ApiResponse(data={
        "user": {"id": user_id, "username": username, "display_name": display_name},
    })


@router.post("/auth/login")
async def login(request: Request, response: Response, payload: AuthLoginRequest) -> ApiResponse[dict]:
    """用户登录。"""
    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if not _login_limiter.check(client_ip):
        raise AppException("登录请求过于频繁，请稍后再试", code="rate_limit_exceeded", http_status=429)

    username = payload.username.strip()
    password = payload.password

    db = Database.get()
    user = await db.user_get_by_username(username)

    if not user or not verify_password(password, user["password_hash"]):
        raise AppException("用户名或密码错误", code="invalid_credentials", http_status=401)

    # 生成 token 并设置 cookie
    access_token = create_access_token(user["id"], user["username"])
    refresh_token = create_refresh_token(user["id"])
    set_auth_cookies(response, access_token, refresh_token, secure=is_secure_request(request))

    logger.info("User logged in: %s (%s)", username, user["id"])

    return ApiResponse(data={
        "user": {"id": user["id"], "username": user["username"], "display_name": user["display_name"]},
    })


@router.post("/auth/refresh")
async def refresh(request: Request, response: Response) -> ApiResponse[dict]:
    """刷新 token（refresh_token 从 httpOnly cookie 读取）。"""
    refresh_token = extract_refresh_token_from_request(request)
    if not refresh_token:
        raise AppException("未提供 refresh_token", code="missing_refresh_token", http_status=401)

    token_data = verify_token(refresh_token)
    if token_data.get("type") != "refresh":
        raise AppException("无效的 refresh_token", code="invalid_refresh_token", http_status=401)

    user_id = token_data["sub"]
    db = Database.get()
    user = await db.user_get_by_id(user_id)
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=401)

    # 生成新的 token 并设置 cookie
    new_access_token = create_access_token(user_id, user["username"])
    new_refresh_token = create_refresh_token(user_id)
    set_auth_cookies(response, new_access_token, new_refresh_token, secure=is_secure_request(request))

    return ApiResponse(data={})


@router.post("/auth/logout")
async def logout(response: Response) -> ApiResponse[dict]:
    """登出，清除认证 cookie。"""
    clear_auth_cookies(response)
    return ApiResponse(data={})


@router.get("/auth/me")
async def get_me(current_user: dict = Depends(get_current_user)) -> ApiResponse[dict]:
    """获取当前用户信息。"""
    db = Database.get()
    user = await db.user_get_by_id(current_user["user_id"])
    if not user:
        raise AppException("用户不存在", code="user_not_found", http_status=404)

    return ApiResponse(data=user)
