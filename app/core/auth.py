"""JWT 认证与密码哈希工具。"""
from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
from fastapi import Depends, Request, Response
from passlib.context import CryptContext

from .exceptions import AppException

logger = logging.getLogger(__name__)

# 密码哈希上下文（使用 pbkdf2_sha256，避免 bcrypt 版本兼容问题）
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# JWT 配置
# 密钥必须跨重启稳定，否则每次重启都会让所有已签发 token（含 refresh_token）失效，
# 用户会被强制登出。优先用环境变量 JWT_SECRET；否则持久化到 app/data/.jwt_secret。


def _load_env_minimal() -> None:
    """最小化读取 .env 注入 os.environ（不覆盖已有），避免依赖 config 模块的导入顺序。"""
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        return
    for _line in env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _v = _v.strip()
        # 与 config.py 的 dotenv 解析对齐：剥掉成对包裹引号，否则脚本入口
        # （先 import auth）会把字面引号注入 os.environ（如 JWT_SECRET 带引号签名）。
        if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
            _v = _v[1:-1]
        os.environ.setdefault(_k.strip(), _v)


def _resolve_jwt_secret() -> str:
    _load_env_minimal()
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        return env_secret
    secret_file = Path(__file__).resolve().parent.parent / "data" / ".jwt_secret"
    try:
        if secret_file.exists():
            return secret_file.read_text(encoding="utf-8").strip()
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        _new = secrets.token_hex(32)
        secret_file.write_text(_new, encoding="utf-8")
        logger.info("JWT_SECRET 未设置，已生成并持久化到 %s", secret_file)
        return _new
    except OSError:
        logger.warning("JWT_SECRET 未设置且无法持久化，使用随机密钥 — 重启后所有 token 失效")
        return secrets.token_hex(32)


JWT_SECRET = _resolve_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_ACCESS_TOKEN_EXPIRE_SECONDS = 24 * 60 * 60  # 24 小时
JWT_REFRESH_TOKEN_EXPIRE_SECONDS = 7 * 24 * 60 * 60  # 7 天


def hash_password(password: str) -> str:
    """对密码进行哈希（pbkdf2_sha256，见 pwd_context）。"""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码是否与哈希匹配。"""
    return pwd_context.verify(password, password_hash)


# ============ Token 撤销黑名单 ============
# 登出时把 token 的 jti 加入黑名单，verify_token 检查命中即拒绝。
# 存 (jti, exp) 而非裸 jti：清理线程能按 exp 过期移除，避免 set 无限增长。
# 内存 + prefs KV 持久：进程内查内存（零开销），登出时写 KV、启动时回灌——
# JWT 密钥刻意跨重启持久（保住 refresh 会话），黑名单若纯内存，重启后
# 已登出的 24h access / 7d refresh token 会整体复活。
_revoked_tokens: dict[str, int] = {}  # jti → exp（unix 秒）
_revoked_lock = threading.Lock()

# 撤销记录的 KV 持久化 scope（emoji_preferences 表复用作通用 KV）
_REVOKED_KV_SCOPE = "token_revoke"


def revoke_token(payload: dict[str, Any]) -> None:
    """把一个 token 标记为已撤销（登出时调用）。按 jti + exp 入表。"""
    jti = payload.get("jti")
    if not jti:
        return
    exp = payload.get("exp", int(time.time()) + 86400)
    with _revoked_lock:
        _revoked_tokens[jti] = exp
        # 顺手清理已过期的项（懒惰清理，无需独立线程）
        now = int(time.time())
        expired = [k for k, e in _revoked_tokens.items() if e <= now]
        for k in expired:
            _revoked_tokens.pop(k, None)


def is_revoked(jti: str | None) -> bool:
    """检查 jti 是否在黑名单且未过期。过期项视为未撤销（token 本身已失效）。"""
    if not jti:
        return False
    with _revoked_lock:
        return jti in _revoked_tokens


async def revoke_token_persisted(payload: dict[str, Any]) -> None:
    """撤销 + KV 持久化：登出后即使重启进程，已登出 token 依然被拒。

    持久化失败只降级为内存撤销（当前进程内仍有效），不阻塞登出流程。
    """
    revoke_token(payload)
    jti = payload.get("jti")
    try:
        exp = int(payload.get("exp", 0) or 0)
    except (TypeError, ValueError):
        exp = 0
    if not jti or not exp:
        return
    try:
        from .database import Database
        await Database.get().emoji_pref_upsert(_REVOKED_KV_SCOPE, jti, str(exp))
    except Exception:  # noqa: BLE001
        logger.warning("Failed to persist revoked token (memory-only revocation)", exc_info=True)


async def load_revoked_tokens() -> int:
    """启动回灌：把 KV 里未过期的撤销记录装回内存黑名单，顺带清过期行。

    每请求仍只查内存（is_revoked 零 DB 开销）：DB 只在登出时写、启动时读。
    """
    try:
        from .database import Database
        rows = await Database.get().prefs_get_by_scope(_REVOKED_KV_SCOPE)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to load revoked tokens from KV", exc_info=True)
        return 0
    now = int(time.time())
    loaded = 0
    stale: list[str] = []
    for jti, exp_raw in rows.items():
        try:
            exp = int(exp_raw)
        except (TypeError, ValueError):
            stale.append(jti)
            continue
        if exp > now:
            with _revoked_lock:
                _revoked_tokens[jti] = exp
            loaded += 1
        else:
            stale.append(jti)
    for jti in stale:
        try:
            await Database.get().emoji_pref_delete(_REVOKED_KV_SCOPE, jti)
        except Exception:  # noqa: BLE001
            pass
    return loaded


def create_access_token(user_id: str, username: str) -> str:
    """创建访问 token（短期）。"""
    payload = {
        "sub": user_id,
        "username": username,
        "type": "access",
        "exp": int(time.time()) + JWT_ACCESS_TOKEN_EXPIRE_SECONDS,
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """创建刷新 token（长期）。"""
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": int(time.time()) + JWT_REFRESH_TOKEN_EXPIRE_SECONDS,
        "iat": int(time.time()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict[str, Any]:
    """验证并解析 JWT token。

    Returns:
        payload 字典

    Raises:
        AppException: token 无效、过期、或已撤销（登出黑名单）
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AppException("Token 已过期", code="token_expired", http_status=401)
    except jwt.InvalidTokenError:
        raise AppException("无效的 Token", code="invalid_token", http_status=401)
    # 黑名单检查：登出后的 token 即使未过期也拒绝
    if is_revoked(payload.get("jti")):
        raise AppException("Token 已撤销", code="token_revoked", http_status=401)
    return payload


# Cookie 配置
ACCESS_COOKIE = "aether_token"
REFRESH_COOKIE = "aether_refresh_token"
COOKIE_MAX_AGE_ACCESS = JWT_ACCESS_TOKEN_EXPIRE_SECONDS
COOKIE_MAX_AGE_REFRESH = JWT_REFRESH_TOKEN_EXPIRE_SECONDS


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    *,
    secure: bool = False,
) -> None:
    """在 response 上设置 httpOnly cookie。

    secure=True 时 cookie 仅通过 HTTPS 下发；明文 HTTP 部署传 False（默认），
    否则浏览器不会回传 cookie 导致登录失效。
    """
    response.set_cookie(
        key=ACCESS_COOKIE, value=access_token,
        httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE_ACCESS,
        secure=secure, path="/",
    )
    response.set_cookie(
        key=REFRESH_COOKIE, value=refresh_token,
        httponly=True, samesite="lax", max_age=COOKIE_MAX_AGE_REFRESH,
        secure=secure, path="/",
    )


def is_secure_request(request: Request) -> bool:
    """判断当前请求是否走 HTTPS。

    优先读反代下发的 X-Forwarded-Proto（Nginx/Caddy 等会设置），
    其次看 request.url.scheme。明文 HTTP（含局域网 / Tailscale HTTP）返回 False。
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded_proto:
        return forwarded_proto == "https"
    return request.url.scheme == "https"


def clear_auth_cookies(response: Response) -> None:
    """清除认证 cookie。"""
    response.delete_cookie(key=ACCESS_COOKIE, path="/")
    response.delete_cookie(key=REFRESH_COOKIE, path="/")


def extract_token_from_request(request: Request) -> str | None:
    """从请求中提取 token，优先级：Authorization header > cookie。

    曾支持 query param（?token=）作为第三优先级，已移除：URL 中的 token
    会进浏览器历史与反代/访问日志。脚本调用请改用 Authorization header。
    """
    # 1. Authorization header
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    # 2. Cookie
    return request.cookies.get(ACCESS_COOKIE)


def extract_refresh_token_from_request(request: Request) -> str | None:
    """从请求中提取 refresh token（仅 cookie；body 通道从未实现）。"""
    return request.cookies.get(REFRESH_COOKIE)


async def get_current_user(
    request: Request,
) -> dict[str, str]:
    """FastAPI 依赖注入：从请求中提取当前用户信息。

    支持两种方式：Authorization header > httpOnly cookie。
    认证中间件已解码的 payload 挂在 request.state.jwt_payload，直接复用，
    避免每请求二次 JWT decode；APP_TOKEN 直通等未走中间件解码的路径回退自取。
    """
    payload = getattr(request.state, "jwt_payload", None)
    if not isinstance(payload, dict):
        token = extract_token_from_request(request)
        if not token:
            raise AppException("未提供认证信息", code="missing_auth", http_status=401)
        payload = verify_token(token)

    # 仅 access token 可用于访问 API；refresh token 只能用于 /api/auth/refresh
    if payload.get("type") != "access":
        raise AppException("无效的 Token 类型", code="invalid_token_type", http_status=401)

    return {
        "user_id": payload["sub"],
        "username": payload.get("username", ""),
    }


async def get_current_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """FastAPI 依赖注入：要求当前用户是管理员。

    用于危险接口的权限分级（安全审计 2B）：插件上传/删除、HA 连接配置、
    模拟器开关、运维操作、二级密码管理。首注册用户自动成为管理员。
    """
    from .database import Database

    try:
        db = Database.get()
        user = await db.user_get_by_id(current_user["user_id"])
    except Exception:
        user = None
    if not user or not user.get("is_admin"):
        raise AppException(
            "该操作需要管理员权限（首个注册的用户）",
            code="admin_required", http_status=403,
        )
    return {**current_user, "is_admin": 1}


async def require_owned_session(
    container: Any, session_id: str, current_user: dict
) -> Any:
    """校验会话归属当前用户并返回 SessionState；不存在 404、不归属 403。

    与 get_current_user/get_current_admin 一样属于路由层的授权依赖，故放在这里；
    container 用鸭子类型（只取 .session_store）以避免 core ← container 的循环导入。
    凡是按 session_id 读写会话内部状态（消息、待确认草稿）的端点都必须过这道校验。
    """
    session = await container.session_store.get_session(session_id)
    if session is None:
        raise AppException("会话不存在", code="session_not_found", http_status=404)
    if session.user_id and session.user_id != current_user["user_id"]:
        raise AppException("无权访问该会话", code="forbidden", http_status=403)
    return session


