"""规则路由 — 自动化规则的 CRUD。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.exceptions import AppException
from ..schema.api_schemas import (
    RuleCreateRequest,
    RulePayloadRequest,
    RuleEnabledRequest,
    RuleReviseRequest,
    RuleUpdateRequest,
    ExplainRequest,
)

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
    rule = await container.rule_service.build_rule(text, user_id=user_id, camera_id=payload.camera_id)
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


@router.post("/rules/{rule_id}/revise")
async def revise_rule(
    rule_id: str,
    payload: RuleReviseRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """对话式修改已有规则（不落库）。

    前端把当前规则 JSON + 修改指令发来，后端用 LLM 输出新 JSON 预览。
    支持多轮：每轮的 current 是上一轮的输出，保证上下文连续。
    """
    # 优先用请求体里的 current（前端维护的多轮状态），兜底查 DB
    current = payload.current or {}
    if not current:
        stored = container.rule_registry_service.get_rule(rule_id)
        if stored is None:
            raise AppException(f"规则不存在: {rule_id}", code="rule_not_found", http_status=404)
        current = stored

    try:
        result = await container.rule_service.revise_rule(
            current, payload.instruction, user_id=current_user.get("user_id", "")
        )
    except Exception as e:
        logger.warning("revise_rule failed: %s", e, exc_info=True)
        return ApiResponse(success=False, message=f"修改失败：{e}", data=None)
    return ApiResponse(data=result)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    payload: RuleUpdateRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """把 revise 后确认的规则 JSON 落库。"""
    try:
        stored = container.rule_registry_service.update_rule(rule_id, payload.rule)
    except AppException:
        raise
    except Exception as e:
        logger.warning("update_rule failed: %s", e, exc_info=True)
        return ApiResponse(success=False, message=f"保存失败：{e}", data=None)
    return ApiResponse(data=stored)


@router.post("/rules/{rule_id}/explain")
async def explain_rule(
    rule_id: str,
    payload: ExplainRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """plan 模式：用自然语言回答关于当前规则的提问（只读，不修改）。

    前端传当前规则 JSON + 问题，后端用 LLM 生成解释。
    """
    current = payload.current or {}
    if not current:
        stored = container.rule_registry_service.get_rule(rule_id)
        if stored is None:
            raise AppException(f"规则不存在: {rule_id}", code="rule_not_found", http_status=404)
        current = stored
    try:
        answer = await container.rule_service.explain_rule(
            current, payload.question, user_id=current_user.get("user_id", "")
        )
    except Exception as e:
        logger.warning("explain_rule failed: %s", e, exc_info=True)
        return ApiResponse(success=False, message=f"解释失败：{e}", data=None)
    return ApiResponse(data={"answer": answer})
