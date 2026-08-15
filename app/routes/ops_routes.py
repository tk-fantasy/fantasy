"""运维路由（09 清单条目 1）：诊断包导出 + 审计查询。

GET /api/ops/diagnostics — 生成并下载脱敏诊断包（zip），操作写审计
GET /api/ops/audit       — 最近运维审计记录（排障时核对谁导出过）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..ops import audit
from ..ops.diag import build_diagnostic_package

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ops/diagnostics")
async def export_diagnostics(
    current_user: dict = Depends(get_current_user),
) -> Response:
    data, filename = await build_diagnostic_package(
        operator=current_user.get("username") or current_user["user_id"]
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ops/audit")
async def recent_audit(
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[list[dict]]:
    return ApiResponse(data=audit.tail(limit=50))
