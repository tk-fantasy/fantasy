"""规则路由 — 自动化规则的 CRUD。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.exceptions import AppException
from ..schema.api_schemas import RuleCreateRequest, RulePayloadRequest, RuleEnabledRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/task/rule")
async def build_rule(
    payload: RuleCreateRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    user_id = current_user.get("user_id", "")
    text = payload.text
    rule = await container.rule_service.build_rule(text, user_id=user_id)
    condition = str(rule.get("condition", "")).strip()
    if not condition:
        return ApiResponse(
            success=False,
            message="无法从输入中解析出有效的视觉条件",
            data=None,
        )
    stored = container.rule_registry_service.add_rule(rule, user_id=user_id)
    return ApiResponse(data=stored)


@router.get("/rules")
async def list_rules(container: AppContainer = Depends(get_container)) -> ApiResponse[list[dict]]:
    return ApiResponse(data=container.rule_registry_service.list_rules())


@router.post("/rules")
async def create_rule(
    payload: RulePayloadRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    condition = payload.condition.strip()
    if not condition:
        return ApiResponse(
            success=False,
            message="规则必须包含 condition 字段",
            data=None,
        )
    rule_dict = payload.model_dump()
    rule_dict.setdefault("enabled", True)
    rule_dict.setdefault("cooldown_seconds", 10)
    return ApiResponse(data=container.rule_registry_service.add_rule(rule_dict, user_id=current_user.get("user_id", "")))


@router.post("/rules/{rule_id}/enabled")
async def set_rule_enabled(
    rule_id: str,
    payload: RuleEnabledRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    return ApiResponse(data=container.rule_registry_service.set_enabled(rule_id, payload.enabled))


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    return ApiResponse(data=container.rule_registry_service.delete_rule(rule_id))
