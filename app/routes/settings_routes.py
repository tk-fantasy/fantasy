"""设置相关路由（LLM Keys, 模型设置, 视觉关注等）。"""
from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.config import delete_llm_key, upsert_llm_key
from ..core.database import Database
from ..core.exceptions import AppException
from ..schema.api_schemas import (
    LLMKeyRequest,
    LLMSettingsRequest,
    VisionFocusesCreateRequest,
    VisionFocusesUpdateRequest,
    VisionFocusRequest,
)
from ..services.model_test_service import test_model_connection

logger = logging.getLogger(__name__)

router = APIRouter()


def _reload_key_pools(container: AppContainer) -> None:
    """llm_keys 改动后重建 key 池，并检测 embed 模型变更触发 RAG 重建。"""
    container.vision_key_pool.reload()
    container.embed_client.reload()
    if container.rag_service:
        container.rag_service.maybe_rebuild_if_model_changed()


async def _sync_llm_keys_to_current_user(current_user: dict) -> None:
    """同步当前 config 的 llm_keys 到指定用户的 user_settings。"""
    try:
        from ..core.config import get_config
        db = Database.get()
        await db.user_setting_set(
            current_user["user_id"],
            "llm_keys",
            json.dumps(get_config("llm_keys", []), ensure_ascii=False)
        )
    except Exception as e:
        logger.warning(f"Failed to sync llm_keys to user: {e}")


def _generate_key_id(base_url: str) -> str:
    """从 base_url 生成 key ID。"""
    parsed = urlparse(base_url)
    host = parsed.hostname or "unknown"
    # 取域名第一部分作为前缀
    parts = host.split(".")
    prefix = parts[0] if parts else host
    # 加上随机后缀
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}-{suffix}"


@router.get("/llm_keys")
async def list_llm_keys(current_user: dict = Depends(get_current_user)) -> ApiResponse[list[dict]]:
    """列出当前用户的 LLM Keys（不含密钥值）。"""
    db = Database.get()
    llm_keys_json = await db.user_setting_get(current_user["user_id"], "llm_keys")
    if not llm_keys_json:
        return ApiResponse(data=[])
    keys = json.loads(llm_keys_json)
    # 返回时隐藏实际 API key，和 list_llm_keys_public 格式一致
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

    # 自动处理 base_url：本地地址补 /v1
    parsed = urlparse(base_url)
    is_local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if is_local and not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    # 根据 type 自动设置 path
    if model_type == "embed":
        chat_path = ""
        embed_path = "/embeddings"
    elif model_type == "stt":
        # STT 走 /audio/transcriptions，由 stt_service 硬编码，不使用 chat/embed path
        chat_path = ""
        embed_path = ""
    else:
        chat_path = "/chat/completions"
        embed_path = ""

    # 新增时自动生成 id
    is_new = not key_id
    if is_new:
        key_id = _generate_key_id(base_url)

    # 新增时自动测试连接（STT 无标准 chat/embed 端点，跳过测试）
    if is_new and api_key and model_type != "stt":
        test_result = await test_model_connection(
            base_url=base_url,
            model=model,
            role=model_type,
            api_key=api_key,
            embed_path=embed_path,
        )
        if not test_result.get("ok"):
            raise AppException(
                f"连接测试失败: {test_result.get('error', '未知错误')}",
                code="llm_key_test_failed",
                http_status=400,
            )

    entry = {
        "id": key_id,
        "base_url": base_url,
        "model": model,
        "type": model_type,
        "chat_path": chat_path,
        "embed_path": embed_path,
    }

    keys = upsert_llm_key(entry, api_key_value=api_key if api_key else None)
    _reload_key_pools(container)

    # 同步到当前用户的 user_settings
    await _sync_llm_keys_to_current_user(current_user)

    return ApiResponse(data=keys)


@router.delete("/llm_keys/{key_id}")
async def delete_llm_key_route(
    key_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    """删除 LLM Key。"""
    keys = delete_llm_key(key_id)
    _reload_key_pools(container)

    # 同步到当前用户的 user_settings
    await _sync_llm_keys_to_current_user(current_user)

    return ApiResponse(data=keys)


@router.get("/llm/settings")
async def get_llm_settings(
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """获取当前 LLM 设置。"""
    return ApiResponse(data={
        "current": container.llm_settings_service.current_settings(),
        "warnings": container.llm_settings_service.warnings(),
    })


@router.post("/llm/settings")
async def set_llm_settings(
    payload: LLMSettingsRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """应用 LLM 设置。"""
    result = container.llm_settings_service.apply(
        role=payload.role,
        key_id=payload.key_id,
        max_concurrency=payload.max_concurrency,
        thinking=payload.thinking,
        multimodal=payload.multimodal,
    )

    # 同步 providers 到当前用户的 user_settings
    try:
        from ..core.config import get_config
        db = Database.get()
        providers = get_config("providers", {})
        await db.user_setting_set(
            current_user["user_id"],
            "providers",
            json.dumps(providers, ensure_ascii=False)
        )
    except Exception as e:
        logger.warning(f"Failed to sync providers to user: {e}")

    return ApiResponse(data=result)


@router.get("/vision/focus")
async def get_vision_focus(
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """获取当前视觉关注指令（兼容旧接口，返回第一条）。"""
    return ApiResponse(data={"focus": container.vision_service.get_vision_focus()})


@router.post("/vision/focus")
async def set_vision_focus(
    payload: VisionFocusRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """设置视觉关注指令（兼容旧接口，追加一条）。"""
    focus = payload.focus.strip()
    container.vision_service.set_vision_focus(focus)
    db = Database.get()
    await db.kv_set("vision_focuses", json.dumps(container.vision_service.get_vision_focuses()))
    return ApiResponse(data={"focus": container.vision_service.get_vision_focus()})


# ---- 多条 focus CRUD ----

@router.get("/vision/focuses")
async def get_vision_focuses(
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    """获取所有视觉关注项。"""
    return ApiResponse(data=container.vision_service.get_vision_focuses())


@router.post("/vision/focuses")
async def add_vision_focus(
    payload: VisionFocusesCreateRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """新增一条视觉关注。"""
    text = payload.text.strip()
    item = container.vision_service.add_focus(text)
    db = Database.get()
    await db.kv_set("vision_focuses", json.dumps(container.vision_service.get_vision_focuses()))
    return ApiResponse(data=item)


@router.put("/vision/focuses/{focus_id}")
async def update_vision_focus(
    focus_id: str,
    payload: VisionFocusesUpdateRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """更新一条视觉关注。"""
    text = payload.text
    enabled = payload.enabled
    item = container.vision_service.update_focus(focus_id, text=text, enabled=enabled)
    if item is None:
        raise AppException("focus 不存在", code="vision_focus_not_found", http_status=404)
    db = Database.get()
    await db.kv_set("vision_focuses", json.dumps(container.vision_service.get_vision_focuses()))
    return ApiResponse(data=item)


@router.delete("/vision/focuses/{focus_id}")
async def delete_vision_focus(
    focus_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """删除一条视觉关注。"""
    ok = container.vision_service.delete_focus(focus_id)
    if not ok:
        raise AppException("focus 不存在", code="vision_focus_not_found", http_status=404)
    db = Database.get()
    await db.kv_set("vision_focuses", json.dumps(container.vision_service.get_vision_focuses()))
    return ApiResponse(data={"deleted": True})
