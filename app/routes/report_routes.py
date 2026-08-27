"""家庭报告路由 — 事件历史（告警/任务/自动化）+ 周报。

数据源是 family_events 表（alert_service 与各 hook 点写入）。周报生成默认
关闭（weekly_report.enabled），此处提供手动触发入口与最近报告查询。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Query

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.database import Database

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/events")
async def list_events(
    days: int = Query(default=7, ge=1, le=90),
    kind: str = Query(default=""),
) -> ApiResponse[list[dict]]:
    """近 N 天家庭事件流（可按 kind 前缀过滤，如 kind=alert）。"""
    since = int((time.time() - days * 24 * 3600) * 1000)
    events = await Database.get().family_events_since(since)
    if kind:
        events = [e for e in events if e["kind"].startswith(kind)]
    # 最新在前（前端时间线习惯）
    return ApiResponse(data=list(reversed(events[-500:])))


@router.get("/report/weekly")
async def get_weekly_report(container: AppContainer = Depends(get_container)) -> ApiResponse[dict | None]:
    svc = container.weekly_report_service
    if svc is None:
        return ApiResponse(data=None)
    return ApiResponse(data=await svc.latest_report())


@router.post("/report/weekly/generate")
async def generate_weekly_report(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """手动生成一份周报（管理员日常维护用，无权限门槛——家庭共享）。"""
    svc = container.weekly_report_service
    if svc is None:
        return ApiResponse(success=False, message="周报服务未就绪", data=None)
    try:
        result = await svc.generate()
        return ApiResponse(data=result)
    except Exception as e:  # noqa: BLE001
        logger.exception("manual weekly report generation failed")
        return ApiResponse(success=False, message=f"生成失败: {e}", data=None)
