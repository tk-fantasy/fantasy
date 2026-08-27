"""全局 LLM Key 配置路由（二级密码门禁）。

从 settings_routes.py 拆出。端点路径不变。全局 key 存 config.json 顶层
llm_keys 数组（跨重启持久化、所有用户共享），用二级密码门禁。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from urllib.parse import urlparse

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_admin, get_current_user, hash_password
from ..core.config import (
    get_config,
    save_global_llm_keys,
    set_secondary_password_hash,
    write_secrets,
)
from ..core.exceptions import AppException
from ..core.rate_limit import RateLimiter
from ..schema.api_schemas import (
    GlobalLLMKeyRequest,
    GlobalLLMSettingsRequest,
    SecondaryPasswordSetupRequest,
    SecondaryPasswordVerifyRequest,
)
from ..services import llm_key_service
from ..services.model_test_service import test_model_connection

logger = logging.getLogger(__name__)

router = APIRouter()

# 二级密码验证限流：全局 key 的门禁，不能无限速暴力破解（与登录同规格 5 次/分钟/IP）
_verify_limiter = RateLimiter(max_requests=5, window_seconds=60)
# 写接口（带密码的保存/删除/设置）独立限流：比 verify 宽（连续保存多个 key 的
# 正常操作不被卡），但仍远低于可爆破速率（PBKDF2 29k 轮 + 10 次/分钟 ≈ 60ms/次
# 的服务端哈希成本让在线爆破不可行）
_write_limiter = RateLimiter(max_requests=10, window_seconds=60)


def _check_write_limited(request: Request) -> None:
    """写接口共用：按 IP 限流，超限抛 429。防绕过 verify 接口直接爆破写接口。"""
    client_ip = request.client.host if request.client else "unknown"
    if not _write_limiter.check(client_ip):
        logger.warning("Secondary password write rate limited: %s", client_ip)
        raise AppException(
            "尝试过于频繁，请稍后再试", code="rate_limited", http_status=429
        )

# 全局 key 热重载开关：True=改全局 chat key 后自动 rebuild agent；
# 若 httpx 客户端误关导致在线请求断，改 False 退回"提示重启"。
GLOBAL_KEY_HOT_RELOAD = True


@router.get("/global/password/status")
async def get_global_password_status() -> ApiResponse[dict]:
    """查询是否已设置二级密码（不暴露哈希）。"""
    return ApiResponse(data={"set": llm_key_service.is_secondary_password_set()})


@router.post("/global/password")
async def set_global_password(
    payload: SecondaryPasswordSetupRequest,
    current_user: dict = Depends(get_current_admin),
) -> ApiResponse[dict]:
    """首次设置二级密码。若已设置则 409，需先调用 DELETE /global/password 重置。"""
    if llm_key_service.is_secondary_password_set():
        raise AppException(
            "二级密码已设置，无法重复设置。如需重置请先调用重置接口（DELETE /global/password）",
            code="secondary_password_already_set",
            http_status=409,
        )
    h = hash_password(payload.password)
    set_secondary_password_hash(h)
    logger.info("Secondary password set", extra={"user_id": current_user["user_id"]})
    return ApiResponse(data={"set": True})


@router.post("/global/password/verify")
async def verify_global_password(
    payload: SecondaryPasswordVerifyRequest,
    request: Request,
) -> ApiResponse[dict]:
    """验证二级密码。前端解锁全局配置面板用，无状态（每次写操作都要再带一次密码）。"""
    client_ip = request.client.host if request.client else "unknown"
    if not _verify_limiter.check(client_ip):
        logger.warning("Secondary password verify rate limited: %s", client_ip)
        raise AppException(
            "尝试过于频繁，请稍后再试", code="rate_limited", http_status=429
        )
    llm_key_service.verify_secondary_password(payload.password)
    return ApiResponse(data={"verified": True})


@router.delete("/global/password")
async def reset_global_password(
    current_user: dict = Depends(get_current_admin),
) -> ApiResponse[dict]:
    """重置（清除）二级密码。仅管理员（自救入口收窄到户主，防家庭成员接管全局 key）。"""
    if not llm_key_service.is_secondary_password_set():
        raise AppException(
            "二级密码未设置，无需重置",
            code="secondary_password_not_set",
            http_status=409,
        )
    set_secondary_password_hash("")
    logger.info("Secondary password reset", extra={"user_id": current_user["user_id"]})
    return ApiResponse(data={"reset": True})


@router.get("/global/llm_keys")
async def list_global_llm_keys(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    """列出全局 LLM Keys（隐藏明文密钥）。读操作不需二级密码。"""
    keys = list(get_config("llm_keys", []) or [])
    return ApiResponse(data=llm_key_service.mask_global_keys(keys))


@router.post("/global/llm_keys")
async def upsert_global_llm_key_route(
    payload: GlobalLLMKeyRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """新增或更新全局 LLM Key。写 config.json + .env，触发热重载。需二级密码。"""
    _check_write_limited(request)
    if not llm_key_service.is_secondary_password_set():
        raise AppException(
            "尚未设置全局配置二级密码，请先调用 /api/global/password 设置",
            code="secondary_password_not_set",
            http_status=403,
        )
    llm_key_service.verify_secondary_password(payload.password)

    base_url = payload.base_url.strip()
    model = payload.model.strip()
    model_type = payload.type.strip()
    api_key = payload.api_key.strip()
    key_id = payload.id.strip()

    if model_type not in ("chat", "summary", "vision", "embed", "stt"):
        raise AppException(
            "type 必须是 chat/summary/vision/embed/stt 之一",
            code="llm_key_invalid", http_status=400,
        )

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

    env_name = f"LLM_KEY_{key_id.upper().replace('-', '_')}"

    existing_keys = list(get_config("llm_keys", []) or [])
    existing = next((k for k in existing_keys if k.get("id") == key_id), None)
    if existing:
        env_name = existing.get("api_key_env", env_name)
        if not api_key:
            pass  # 留空 = 不改密钥，沿用 env 现有值
        else:
            write_secrets({env_name: api_key})
    else:
        if not api_key:
            raise AppException(
                "新增 key 必须提供 api_key",
                code="llm_key_missing_api_key", http_status=400,
            )
        write_secrets({env_name: api_key})

    entry = {
        "id": key_id, "base_url": base_url, "model": model, "type": model_type,
        "chat_path": chat_path, "embed_path": embed_path, "api_key_env": env_name,
    }

    replaced = False
    for i, k in enumerate(existing_keys):
        if k.get("id") == key_id:
            existing_keys[i] = entry
            replaced = True
            break
    if not replaced:
        existing_keys.append(entry)

    saved = save_global_llm_keys(existing_keys)
    llm_key_service.reload_key_pools(container)

    restart_required = False
    if GLOBAL_KEY_HOT_RELOAD and model_type == "chat":
        try:
            from ..main import _rebuild_agent, _rebuild_lock
            async with _rebuild_lock:
                await _rebuild_agent()
        except Exception:
            logger.exception("Global chat key hot-reload failed, suggesting restart")
            restart_required = True

    logger.info("Global LLM key saved", extra={
        "user_id": current_user["user_id"], "key_id": key_id, "type": model_type,
        "restart_required": restart_required,
    })
    return ApiResponse(data={
        "keys": llm_key_service.mask_global_keys(saved),
        "restart_required": restart_required,
    })


@router.delete("/global/llm_keys/{key_id}")
async def delete_global_llm_key_route(
    key_id: str,
    payload: SecondaryPasswordVerifyRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """删除全局 LLM Key（.env 密钥保留，避免误伤其他引用）。需二级密码。"""
    _check_write_limited(request)
    llm_key_service.verify_secondary_password(payload.password)
    keys = [k for k in (get_config("llm_keys", []) or []) if k.get("id") != key_id]
    saved = save_global_llm_keys(keys)
    llm_key_service.reload_key_pools(container)
    logger.info("Global LLM key deleted", extra={
        "user_id": current_user["user_id"], "key_id": key_id,
    })
    return ApiResponse(data={"keys": llm_key_service.mask_global_keys(saved)})


@router.get("/global/llm/settings")
async def get_global_llm_settings(
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """获取全局 providers 设置（5 个角色都从 config.json 读，全局共享）。"""
    settings = container.llm_settings_service.current_settings()
    return ApiResponse(data={
        "current": settings,
        "warnings": container.llm_settings_service.warnings(),
    })


@router.post("/global/llm/settings")
async def set_global_llm_settings(
    payload: GlobalLLMSettingsRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """写全局 providers 到 config.json（所有用户共享）。需二级密码。"""
    _check_write_limited(request)
    llm_key_service.verify_secondary_password(payload.password)
    result = container.llm_settings_service.apply(
        role=payload.role, key_id=payload.key_id,
        max_concurrency=payload.max_concurrency,
        thinking=payload.thinking, multimodal=payload.multimodal,
    )

    restart_required = False
    if GLOBAL_KEY_HOT_RELOAD and payload.role == "chat":
        try:
            from ..main import _rebuild_agent, _rebuild_lock
            async with _rebuild_lock:
                await _rebuild_agent()
        except Exception:
            logger.exception("Global chat settings hot-reload failed, suggesting restart")
            restart_required = True

    return ApiResponse(data={**result, "restart_required": restart_required})
