"""数据出网策略路由（09 清单条目 4）。

GET  /api/egress         — 当前模式 + 端点内外网归属 + 声明确认状态（徽标/引导页用）
POST /api/egress         — 切换模式（cloud/hybrid/local），登录即可
POST /api/egress/confirm — 引导页声明确认（记录 hash 到数据库）

鉴权：/api/* 由 api_token_guard 中间件统一要求 JWT；写接口叠加
get_current_user 以拿到操作人留痕。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..schema.api_schemas import EgressConfirmRequest, EgressPolicyRequest
from ..services import egress_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/egress")
async def get_egress_policy() -> ApiResponse[dict]:
    return ApiResponse(data=await egress_service.policy_status())


@router.post("/egress")
async def set_egress_policy(
    payload: EgressPolicyRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    egress_service.set_mode(payload.mode)
    logger.info("Egress mode changed", extra={
        "user_id": current_user["user_id"], "mode": payload.mode,
    })
    return ApiResponse(data=await egress_service.policy_status())


@router.post("/egress/confirm")
async def confirm_egress_declaration(
    payload: EgressConfirmRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    if not payload.acknowledged:
        from ..core.exceptions import AppException

        raise AppException(
            "需勾选「我已知晓并确认」才能提交", code="egress_not_acknowledged", http_status=400
        )
    # 先落模式再记录确认，保证确认记录与生效模式一致
    egress_service.set_mode(payload.mode)
    record = await egress_service.confirm_declaration(
        payload.mode, current_user.get("username") or current_user["user_id"]
    )
    return ApiResponse(data={
        "confirmed": True,
        "confirmed_at": record["confirmed_at"],
        "hash": record["hash"],
    })
