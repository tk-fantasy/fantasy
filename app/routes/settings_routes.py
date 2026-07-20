"""设置相关路由（LLM Keys, 模型设置, 视觉关注等）。"""
from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user, hash_password, verify_password
from ..core.config import (
    delete_llm_key,
    get_config,
    get_secondary_password_hash,
    save_global_llm_keys,
    set_secondary_password_hash,
    upsert_llm_key,
    write_secrets,
)
from ..core.database import Database
from ..core.exceptions import AppException
from ..core.roles import PER_USER_ROLES
from ..schema.api_schemas import (
    GlobalLLMKeyRequest,
    GlobalLLMSettingsRequest,
    LLMKeyRequest,
    LLMSettingsRequest,
    SecondaryPasswordSetupRequest,
    SecondaryPasswordVerifyRequest,
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
    """同步当前 config 的 llm_keys 到指定用户的 user_settings。

    写入 DB 时把实际 api_key 也存入 api_key 字段（从 env 读取），
    供 per-user 解析使用。明文 key 仅存于本地 SQLite，不会返回前端。

    仅同步 PER_USER_ROLES（chat/summary/stt）：vision/embed 全局共享，
    不进 per-user DB。否则 wizard 跑完后 embed key 会以明文冗余进
    per-user DB，而全局 .env 丢失时运行时无法回退（embed_client 走
    全局解析，不读 per-user），表现为 RAG/语义图/emoji 全部 401。
    """
    try:
        import os
        import copy
        # 局部 import 便于测试 patch app.core.config.get_config
        from ..core.config import get_config
        db = Database.get()
        keys = copy.deepcopy(get_config("llm_keys", []))
        # 仅保留 per-user 角色，剔除 vision/embed（全局共享，不该进用户 DB）
        keys = [k for k in keys if k.get("type") in PER_USER_ROLES]
        for k in keys:
            env_name = k.get("api_key_env", "")
            if env_name and not k.get("api_key"):
                k["api_key"] = os.getenv(env_name, "")
        await db.user_setting_set(
            current_user["user_id"],
            "llm_keys",
            json.dumps(keys, ensure_ascii=False)
        )
    except Exception as e:
        logger.warning(f"Failed to sync llm_keys to user: {e}")


async def _save_user_provider(user_id: str, role: str, key_id: str, values: dict) -> None:
    """把 per-user provider 绑定写入用户 DB。"""
    try:
        db = Database.get()
        providers_json = await db.user_setting_get(user_id, "providers")
        providers = json.loads(providers_json) if providers_json else {}
        providers[role] = {**values, "key_id": key_id}
        await db.user_setting_set(
            user_id, "providers",
            json.dumps(providers, ensure_ascii=False)
        )
    except Exception as e:
        logger.warning(f"Failed to save user provider: {e}")


async def _get_user_providers(user_id: str) -> dict:
    """读取用户 DB 中的 providers 绑定。"""
    try:
        db = Database.get()
        providers_json = await db.user_setting_get(user_id, "providers")
        return json.loads(providers_json) if providers_json else {}
    except Exception:
        return {}


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

    # 新增时自动测试连接（chat/summary/vision/embed/stt 均测）
    if is_new and api_key:
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
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """获取当前 LLM 设置。

    chat/summary/stt 返回当前用户的 per-user 绑定；
    vision/embed 返回全局 config.json 绑定。
    """
    settings = container.llm_settings_service.current_settings()
    # per-user 角色用用户 DB 覆盖。用户没配过的角色返回空 key_id —— 不回退全局，
    # 因为全局 config.json 里的 key_id 属于别的用户���key 是 per-user 隔离的），
    # 拿到当前用户的 key 列表里匹配不到，前端会显示"未选择"。
    # 运行时已有 auto_select 回退（resolve_key_for_role_user），不依赖这里返回的 key_id。
    user_providers = await _get_user_providers(current_user["user_id"])
    for role in PER_USER_ROLES:
        if role in user_providers:
            # 确保字段齐全（老数据可能缺 use_global）
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
    """应用 LLM 设置。

    vision/embed 写全局 config.json（所有用户共享）；
    chat/summary/stt 写用户 DB（per-user 隔离）。
    """
    role = payload.role

    if role in PER_USER_ROLES:
        # per-user：写入用户 DB
        values: dict = {
            "key_id": payload.key_id,
            "max_concurrency": max(1, payload.max_concurrency or 8),
            "enabled": True,
        }
        if role in ("chat", "summary") and payload.thinking is not None:
            values["thinking"] = bool(payload.thinking)
        # use_global：True=该角色走全局兜底（清 key_id 由 resolver 拦截 per-user 回退），
        # False=走 per-user。payload.use_global=None 表示不改此标志。
        if payload.use_global is not None:
            values["use_global"] = bool(payload.use_global)
            if payload.use_global:
                # 切到全局时把 key_id 清空，避免残留绑定误导前端显示
                values["key_id"] = ""
        await _save_user_provider(
            current_user["user_id"], role, values.get("key_id", payload.key_id), values
        )
        # 清除该用户的 agent 缓存，下次聊天用新 key 重建
        if hasattr(container.dispatcher, "invalidate_user_agent"):
            container.dispatcher.invalidate_user_agent(current_user["user_id"])
        logger.info("Per-user provider saved", extra={
            "role": role, "user_id": current_user["user_id"],
            "use_global": values.get("use_global"),
        })
        return ApiResponse(data={"role": role, "applied": values})

    # vision/embed：全局写 config.json（现有逻辑）
    result = container.llm_settings_service.apply(
        role=role,
        key_id=payload.key_id,
        max_concurrency=payload.max_concurrency,
        thinking=payload.thinking,
        multimodal=payload.multimodal,
    )

    # 同步 providers 到当前用户的 user_settings
    try:
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


# ==================== 全局 LLM Key 配置（二级密码门禁） ====================
#
# 全局 key 存 config.json 的 llm_keys 顶层数组（跨重启持久化、所有用户共享），
# 与 per-user key（存用户 DB）互补。全局配置涉及所有用户的模型/费用，因此
# 用二级密码门禁：首次设置密码后，每次写全局 key 都要带密码验证。
#
# 角色策略：
#   vision/embed —— 历史上就是全局共享，沿用不变
#   chat/summary/stt —— per-user 优先；用户可在 /model 页切到"全局兜底"（use_global flag）
#
# 详见 plan-sess_6a3fa977.md。


def _verify_secondary_password(password: str) -> None:
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


def _mask_global_keys(keys: list[dict]) -> list[dict]:
    """返回全局 key 列表给前端（隐藏明文 api_key，保留 api_key_env + 是否已配置标记）。"""
    import os as _os
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
            "api_key_set": bool(_os.getenv(env_name)) if env_name else False,
        })
    return out


# 全局 key 热重载开关：True=改全局 chat key 后自动 rebuild agent；
# 若 httpx 客户端误关（B-bug）导致在线请求断，改 False 退回"提示重启"。
GLOBAL_KEY_HOT_RELOAD = True


@router.get("/global/password/status")
async def get_global_password_status() -> ApiResponse[dict]:
    """查询是否已设置二级密码（不暴露哈希）。"""
    return ApiResponse(data={"set": bool(get_secondary_password_hash())})


@router.post("/global/password")
async def set_global_password(
    payload: SecondaryPasswordSetupRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """首次设置二级密码。若已设置则 409，要求走修改流程（暂未提供修改接口，
    丢了密码需手改 config.json 删 security.secondary_password_hash）。"""
    if get_secondary_password_hash():
        raise AppException(
            "二级密码已设置，无法重复设置。如需重置请手动编辑 config.json",
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
) -> ApiResponse[dict]:
    """验证二级密码。前端解锁全局配置面板用，无状态（每次写操作都要再带一次密码）。"""
    _verify_secondary_password(payload.password)
    return ApiResponse(data={"verified": True})


@router.get("/global/llm_keys")
async def list_global_llm_keys(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    """列出全局 LLM Keys（隐藏明文密钥）。读操作不需二级密码。"""
    keys = list(get_config("llm_keys", []) or [])
    return ApiResponse(data=_mask_global_keys(keys))


@router.post("/global/llm_keys")
async def upsert_global_llm_key_route(
    payload: GlobalLLMKeyRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """新增或更新全局 LLM Key。写 config.json + .env，触发热重载。

    需二级密码（首次设置全局 key 前若未设密码，此接口会要求先设置密码）。
    新增时自动测试连接（与 per-user 接口一致）。
    """
    # 若尚未设置二级密码，引导走首次设置流程；否则验证密码
    if not get_secondary_password_hash():
        raise AppException(
            "尚未设置全局配置二级密码，请先调用 /api/global/password 设置",
            code="secondary_password_not_set",
            http_status=403,
        )
    _verify_secondary_password(payload.password)

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

    # 自动处理 base_url：本地地址补 /v1
    parsed = urlparse(base_url)
    is_local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if is_local and not base_url.endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"

    # 根据 type 自动设置 path
    if model_type == "embed":
        chat_path, embed_path = "", "/embeddings"
    elif model_type == "stt":
        chat_path, embed_path = "", ""
    else:
        chat_path, embed_path = "/chat/completions", ""

    is_new = not key_id
    if is_new:
        key_id = _generate_key_id(base_url)

    # 新增时自动测试连接
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

    # 编辑现有 key 且 api_key 留空：保留原 env 的密钥（不覆盖）
    existing_keys = list(get_config("llm_keys", []) or [])
    existing = next((k for k in existing_keys if k.get("id") == key_id), None)
    if existing:
        # 沿用原 env 名（避免改 key 时丢密钥）
        env_name = existing.get("api_key_env", env_name)
        if not api_key:
            # 留空 = 不改密钥，沿用 env 现有值
            pass
        else:
            write_secrets({env_name: api_key})
    else:
        # 新增：必须有 api_key
        if not api_key:
            raise AppException(
                "新增 key 必须提供 api_key",
                code="llm_key_missing_api_key", http_status=400,
            )
        write_secrets({env_name: api_key})

    entry = {
        "id": key_id,
        "base_url": base_url,
        "model": model,
        "type": model_type,
        "chat_path": chat_path,
        "embed_path": embed_path,
        "api_key_env": env_name,
    }

    # 替换或追加
    replaced = False
    for i, k in enumerate(existing_keys):
        if k.get("id") == key_id:
            existing_keys[i] = entry
            replaced = True
            break
    if not replaced:
        existing_keys.append(entry)

    saved = save_global_llm_keys(existing_keys)
    _reload_key_pools(container)

    # 热重载全局 agent（仅 chat 角色变更需要 rebuild，其他角色 reload client 即可）
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
        "keys": _mask_global_keys(saved),
        "restart_required": restart_required,
    })


@router.delete("/global/llm_keys/{key_id}")
async def delete_global_llm_key_route(
    key_id: str,
    password: str = "",
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """删除全局 LLM Key（.env 密钥保留，避免误伤其他引用）。需二级密码。

    DELETE 请求体不便带 JSON，这里用 query 参数 ?password=xxx。
    """
    _verify_secondary_password(password)
    keys = [k for k in (get_config("llm_keys", []) or []) if k.get("id") != key_id]
    saved = save_global_llm_keys(keys)
    _reload_key_pools(container)
    logger.info("Global LLM key deleted", extra={
        "user_id": current_user["user_id"], "key_id": key_id,
    })
    return ApiResponse(data={"keys": _mask_global_keys(saved)})


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
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """写全局 providers 到 config.json（所有用户共享）。需二级密码。"""
    _verify_secondary_password(payload.password)
    result = container.llm_settings_service.apply(
        role=payload.role,
        key_id=payload.key_id,
        max_concurrency=payload.max_concurrency,
        thinking=payload.thinking,
        multimodal=payload.multimodal,
    )

    # 热重载全局 agent（chat 角色变更需要 rebuild）
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
