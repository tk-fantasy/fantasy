"""Home Assistant 路由 — 设备控制、配置、测试。"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Query

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


@router.get("/ha/history")
async def ha_history(
    filter_entity_id: str = Query(..., description="实体 ID（逗号分隔多个）"),
    hours: float = Query(default=24, ge=0.1, le=24 * 30, description="查询近 N 小时历史"),
    minimal: bool = Query(default=True, description="仅返回最少字段以加速传输"),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """查询实体历史状态记录（用于传感器趋势图）。

    后端按 hours 计算起止时间（ISO8601），直接透传 HA /api/history/period。
    """
    from datetime import datetime, timedelta, timezone

    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        timestamp = start.isoformat()
        end_time = now.isoformat()
        history = await container.ha_client.get_history(
            filter_entity_id=filter_entity_id,
            timestamp=timestamp,
            end_time=end_time,
            minimal=minimal,
        )
        return ApiResponse(data={"history": history, "count": len(history)})
    except Exception as e:
        logger.exception("HA history failed")
        raise AppException(f"Home Assistant 历史查询失败: {e}", code="ha_error", http_status=502)


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
        # 调用服务后立即清掉 HAService 的状态缓存，确保前端重拉拿到最新状态
        container.ha_service.invalidate_states_cache()
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


def _classify_ha_error(e: Exception) -> dict:
    """把 HA 调用异常分类成前端可读的 reason。

    返回 {"reason": "unauthorized"|"unreachable"|"error", "detail": str}，
    供 /ha/test 和 /ha/config 保存前预校验共用。
    """
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code in (401, 403):
            return {"reason": "unauthorized", "detail": "Token 无效或已过期（URL 可达，请检查 Token）"}
        return {"reason": "error", "detail": f"HA 返回 HTTP {e.response.status_code}"}
    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, httpx.UnsupportedProtocol)):
        return {"reason": "unreachable", "detail": f"HA 地址不可达：{e}"}
    return {"reason": "error", "detail": str(e)}


@router.post("/ha/config")
async def set_ha_config(payload: HAConfigRequest, container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """保存 HA 配置。

    安全策略：用户传了新 token 时，先用 (url, token) 建临时 client 连一次 HA
    /api/，验证通过才写入 config.json。这样从根上杜绝「存进去的 token 连不上」
    ——用户在页面上误填旧 token / 错 token / 脏文本时，保存会被拒绝而不是把
    好配置覆盖成坏的。
    """
    url = payload.url.strip().rstrip("/")
    new_token = str(payload.token).strip() if payload.token is not None else None

    # 只传了 url（没传 token）：用现有 token 验证 url 是否可达
    # 传了 token：必须验证新 token 真的能连上 HA 才允许保存
    verify_token = new_token if new_token else get_config("ha.token", "")
    if verify_token:
        probe = HomeAssistantClient(base_url=url, token=verify_token)
        try:
            await probe.get_states()
        except Exception as e:
            await probe.close()
            info = _classify_ha_error(e)
            logger.warning("HA config save rejected: %s (%s)", info["reason"], info["detail"])
            return ApiResponse(
                code="ha_error",
                message=info["detail"],
                data={"saved": False, **info},
            )
        finally:
            await probe.close()

    # 验证通过，写盘
    updates: dict = {"url": url}
    if new_token:
        updates["token"] = new_token
    new_cfg = update_config_section("ha", updates)
    old_client = container.ha_client_ref[0]
    new_client = HomeAssistantClient()
    container.ha_client_ref[0] = new_client
    container.ha_service = HAService(client=new_client)
    await old_client.close()
    token_val = new_cfg.get("token", "")
    return ApiResponse(
        data={
            "saved": True,
            "url": new_cfg.get("url", "http://localhost:8123"),
            "token_set": bool(token_val),
            "token_preview": (token_val[:4] + "****" + token_val[-4:]) if len(token_val) >= 8 else ("****" if token_val else ""),
        }
    )


@router.post("/ha/test")
async def test_ha_connection(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """测试 HA 连接。区分 Token 无效 (401) 和地址不可达 (连接/DNS/超时)，
    前端据此给出针对性提示，避免用户误判是 URL 问题还是 Token 问题。
    """
    try:
        states = await container.ha_client.get_states()
        count = len(states)
        return ApiResponse(data={"connected": True, "entity_count": count})
    except Exception as e:
        info = _classify_ha_error(e)
        if info["reason"] != "error":
            logger.warning("HA test failed: %s", info["detail"])
        else:
            logger.exception("HA test connection failed")
        return ApiResponse(
            code="ha_error",
            message=info["detail"],
            data={"connected": False, **info},
        )


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
