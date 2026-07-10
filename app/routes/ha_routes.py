"""Home Assistant 路由 — 设备控制、配置、测试。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..clients.ha_client import HomeAssistantClient
from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section
from ..core.exceptions import AppException
from ..schema.api_schemas import HAConfigRequest, HAServiceCallRequest, ModelTestRequest, UniqueSettingsRequest
from ..services.ha_service import HAService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ha/entities")
async def ha_entities(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    try:
        from ..services.entity_controls import resolve_controls as _rc
        devices = await container.ha_service.get_all_devices()
        raw_svc_defs = await container.ha_service.get_service_defs(
            container.ha_client, domains=set(d.get("domain", "") for d in devices)
        )
        for d in devices:
            d["_controls"] = _rc(d, raw_svc_defs)
        return ApiResponse(data={"entities": devices, "count": len(devices)})
    except Exception as e:
        logger.exception("HA entities failed")
        raise AppException(f"Home Assistant 连接失败: {e}", code="ha_error", http_status=502)


@router.get("/ha/services")
async def ha_services(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """返回 HA 服务定义，格式: {domain: {service_name: {fields: [...], required: [...]}}}"""
    try:
        services_info = await container.ha_service.get_service_defs(
            container.ha_client, include_required=True
        )
        return ApiResponse(data=services_info)
    except Exception as e:
        logger.exception("HA services failed")
        raise AppException(f"Home Assistant 服务列表获取失败: {e}", code="ha_error", http_status=502)


@router.post("/ha/call_service")
async def ha_call_service(payload: HAServiceCallRequest, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    domain = payload.domain
    service = payload.service
    entity_id = payload.entity_id
    data = payload.data
    if entity_id and "." not in str(entity_id):
        entity_id = f"{domain}.{entity_id}"
    try:
        result = await container.ha_client.call_service(domain, service, entity_id, data)
        return ApiResponse(data={"success": True, "result": result})
    except Exception as e:
        logger.exception("HA call_service failed")
        raise AppException(str(e), code="ha_error", http_status=502)


@router.get("/ha/config")
async def get_ha_config() -> ApiResponse[dict]:
    ha_cfg = get_config("ha", {})
    token = ha_cfg.get("token", "")
    return ApiResponse(
        data={
            "url": ha_cfg.get("url", "http://localhost:8123"),
            "token_set": bool(token),
            "token_preview": (token[:4] + "****" + token[-4:]) if len(token) >= 8 else ("****" if token else ""),
        }
    )


@router.post("/ha/config")
async def set_ha_config(payload: HAConfigRequest, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    url = payload.url.strip()
    token = payload.token
    current = get_config("ha", {})
    updates: dict = {}
    if url:
        updates["url"] = url.rstrip("/")
    if token is not None:
        updates["token"] = str(token).strip()
    new_cfg = update_config_section("ha", updates)
    old_client = container.ha_client_ref[0]
    new_client = HomeAssistantClient()
    container.ha_client_ref[0] = new_client
    container.ha_service = HAService(client=new_client)
    await old_client.close()
    token_val = new_cfg.get("token", "")
    return ApiResponse(
        data={
            "url": new_cfg.get("url", "http://localhost:8123"),
            "token_set": bool(token_val),
            "token_preview": (token_val[:4] + "****" + token_val[-4:]) if len(token_val) >= 8 else ("****" if token_val else ""),
        }
    )


@router.post("/ha/test")
async def test_ha_connection(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    try:
        states = await container.ha_client.get_states()
        count = len(states)
        return ApiResponse(data={"connected": True, "entity_count": count})
    except Exception as e:
        logger.exception("HA test connection failed")
        raise AppException(str(e), code="ha_error", http_status=502)


@router.post("/models/test")
async def test_model_connection_route(payload: ModelTestRequest) -> ApiResponse[dict]:
    """测试模型连接。"""
    from ..services.model_test_service import test_model_connection
    result = await test_model_connection(
        base_url=payload.base_url,
        model=payload.model,
        role=payload.role,
        api_key=payload.api_key,
        chat_path=payload.chat_path,
        embed_path=payload.embed_path,
    )
    return ApiResponse(data=result)


@router.get("/unique")
async def get_unique_settings() -> ApiResponse[dict]:
    """获取聊天助手的个性化设置（角色设定、行为原则）。"""
    from ..services.prompt_service import DEFAULT_PERSONA, GUIDELINES
    persona = str(get_config("chat_assistant.persona", "") or "").strip()
    guidelines = str(get_config("chat_assistant.guidelines", "") or "").strip()
    return ApiResponse(
        data={
            "persona": persona or DEFAULT_PERSONA,
            "guidelines": guidelines or GUIDELINES,
            "persona_custom": bool(persona),
            "guidelines_custom": bool(guidelines),
        }
    )


@router.post("/unique")
async def set_unique_settings(payload: UniqueSettingsRequest) -> ApiResponse[dict]:
    """更新聊天助手的个性化设置。仅允许 persona，guidelines 由系统管理。"""
    from ..services.prompt_service import DEFAULT_PERSONA, GUIDELINES
    updates: dict = {}
    if payload.persona:
        updates["persona"] = payload.persona.strip()
    new_cfg = update_config_section("chat_assistant", updates)
    return ApiResponse(
        data={
            "persona": new_cfg.get("persona", "") or DEFAULT_PERSONA,
            "guidelines": GUIDELINES,
            "persona_custom": bool(new_cfg.get("persona", "")),
            "guidelines_custom": False,
        }
    )
