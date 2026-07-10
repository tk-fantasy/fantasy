"""Setup 路由 — 初始配置状态检查。"""
from __future__ import annotations

import json as _json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import extract_token_from_request, verify_token
from ..core.config import get_config, update_config_section
from ..core.database import Database

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def index() -> RedirectResponse:
    return RedirectResponse(url="/landing")


@router.get("/favicon.ico")
async def favicon() -> Response:
    """返回 favicon.ico（优先取前端构建产物，回退 204）。"""
    from pathlib import Path
    from fastapi.responses import FileResponse

    ico_path = Path(__file__).resolve().parent.parent / "static" / "frontend" / "favicon.ico"
    if ico_path.is_file():
        return FileResponse(ico_path, media_type="image/x-icon")
    return Response(status_code=204)


@router.get("/api/setup/status")
async def setup_status(request: Request, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """检查初始配置状态，用于引导用户完成基础配置。

    优先检查当前用户的 user_settings，回退到全局 config。
    """
    ha_client = container.ha_client

    # 验证当前用户（header → cookie → query param）
    current_user = None
    token = extract_token_from_request(request)
    if token:
        try:
            payload = verify_token(token)
            current_user = {"user_id": payload["sub"], "username": payload.get("username", "")}
        except Exception:
            pass

    # 检查 LLM keys（优先用户级，回退全局）
    has_llm_key = False
    llm_key_count = 0
    user_has_llm_keys_setting = False
    if current_user:
        try:
            db = Database.get()
            user_llm_keys = await db.user_setting_get(current_user["user_id"], "llm_keys")
            if user_llm_keys is not None:
                # 用户有 llm_keys 设置（即使是空数组），不再回退全局
                user_has_llm_keys_setting = True
                keys = _json.loads(user_llm_keys)
                llm_key_count = len(keys)
                has_llm_key = llm_key_count > 0
        except Exception:
            pass

    # 回退到全局 config（仅当用户没有 llm_keys 设置时）
    if not user_has_llm_keys_setting:
        llm_keys = get_config("llm_keys", [])
        llm_key_count = len(llm_keys)
        has_llm_key = llm_key_count > 0

    # 检查 HA 配置（全局共享）
    ha_config = get_config("ha", {})
    ha_url = ha_config.get("url", "")
    ha_token = ha_config.get("token", "")
    ha_configured = bool(ha_url and ha_token)
    ha_connected = False

    if ha_configured:
        try:
            states = await ha_client.get_states()
            ha_connected = len(states) > 0
        except Exception:
            ha_connected = False

    # 检查家庭信息（优先用户级，回退全局）
    has_home_info = False
    if current_user:
        try:
            db = Database.get()
            home_info = await db.user_setting_get(current_user["user_id"], "home_info")
            if home_info:
                home_data = _json.loads(home_info)
                has_home_info = bool(home_data.get("home_name") or home_data.get("owner_name"))
        except Exception:
            pass

    if not has_home_info:
        home_config = get_config("home", {})
        has_home_info = bool(home_config.get("home_name") or home_config.get("owner_name"))

    # 判断是否完成初始配置（LLM key + HA token 都需要配置）
    setup_complete = has_llm_key and ha_configured

    return ApiResponse(data={
        "setup_complete": setup_complete,
        "has_llm_key": has_llm_key,
        "ha_configured": ha_configured,
        "ha_connected": ha_connected,
        "has_home_info": has_home_info,
        "llm_key_count": llm_key_count,
    })


class HASetupRequest(BaseModel):
    url: str
    token: str


@router.post("/api/setup/ha")
async def setup_ha(
    body: HASetupRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """保存 HA 连接配置并测试连接。

    需要已登录用户（引导向导在登录后才会到达此步骤）。
    """
    token = extract_token_from_request(request)
    if not token:
        return ApiResponse(code="missing_auth", detail="未提供认证信息")
    try:
        verify_token(token)
    except Exception:
        return ApiResponse(code="invalid_token", detail="认证已过期")

    url = body.url.strip()
    ha_token = body.token.strip()
    if not url or not ha_token:
        return ApiResponse(code="invalid_input", detail="URL 和 Token 不能为空")

    # 写入 config.json 并同步内存
    update_config_section("ha", {"url": url, "token": ha_token})

    # 重建 ha_client 连接并测试
    ha_client = container.ha_client
    ha_client._base_url = url.rstrip("/")
    ha_client._token = ha_token

    ha_connected = False
    entity_count = 0
    try:
        states = await ha_client.get_states()
        entity_count = len(states)
        ha_connected = entity_count > 0
    except Exception as e:
        logger.warning("HA connection test failed: %s", e)

    return ApiResponse(data={
        "ha_connected": ha_connected,
        "entity_count": entity_count,
        "url": url,
    })
