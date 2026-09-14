"""家庭报告路由 — 事件历史（告警/任务/自动化）+ 周报。

数据源是 family_events 表（alert_service 与各 hook 点写入）。周报默认周日
20 点自动生成（weekly_report.enabled，默认开），此处提供手动触发入口与
最近报告查询。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Query

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.database import Database
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/events")
async def list_events(
    days: int = Query(default=7, ge=1, le=90),
    kind: str = Query(default=""),
    date: str = Query(default="", description="YYYY-MM-DD 精确查某一天（本地时区）"),
) -> ApiResponse[list[dict]]:
    """近 N 天家庭事件流（可按 kind 前缀过滤，如 kind=alert）。

    date=YYYY-MM-DD 时精确查某一天（本地时区，图表柱下钻用），days 被忽略。
    500 条截断按类型保底：device_state 等高频事件动辄数百条，纯按时间
    截取会把 automation/task 这类低频重要事件整类挤出时间线（统计图是
    全量聚合不受影响，但用户在时间线里找不到对应条目）。每类先保底
    QUOTA 条（按时间取最新），剩余名额按时间补齐。
    """
    # 直调函数时 FastAPI 参数默认值是 Query() 对象而非字符串，统一归化为 ""
    if not isinstance(date, str):
        date = ""
    if not isinstance(kind, str):
        kind = ""
    if date:
        # 本地时区的 [date 00:00, date+1d 00:00)；格式不符按空查询处理
        import datetime as _dt
        import re as _re
        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return ApiResponse(data=[])
        day_start = int(_dt.datetime.strptime(date, "%Y-%m-%d")
                        .astimezone().timestamp() * 1000)
        since = day_start
        until = day_start + 24 * 3600 * 1000
    else:
        since = int((time.time() - days * 24 * 3600) * 1000)
        until = None
    db = Database.get()
    events = await db.family_events_since(since)
    if until is not None:
        events = [e for e in events if e["created_at"] < until]
    if kind:
        events = [e for e in events if e["kind"].startswith(kind)]

    LIMIT = 500
    QUOTA = 80
    if len(events) > LIMIT and not kind:
        by_kind: dict[str, list[dict]] = {}
        for e in events:
            by_kind.setdefault(e["kind"], []).append(e)
        picked: list[dict] = []
        for group in by_kind.values():
            picked.extend(group[-QUOTA:])  # 每类最新的 QUOTA 条保底
        if len(picked) < LIMIT:
            # 剩余名额按时间从最新往回补（跳过已选）
            chosen_ids = {id(e) for e in picked}
            for e in reversed(events):
                if len(picked) >= LIMIT:
                    break
                if id(e) not in chosen_ids:
                    picked.append(e)
                    chosen_ids.add(id(e))
        events = picked

    # 最新在前（前端时间线习惯）；保底配额会打散时间顺序，需整体重排
    events.sort(key=lambda e: e["created_at"], reverse=True)
    return ApiResponse(data=events[-500:])


@router.get("/events/stats")
async def events_stats(
    days: int = Query(default=7, ge=1, le=90),
) -> ApiResponse[dict]:
    """近 N 天事件统计聚合（家庭报告页图表数据源）。

    返回 totals（按 kind 计数）、daily（按天分桶的堆叠柱状数据）、
    top_devices（device_op 按设备分组，含 AI/手动拆分）、actor（AI/手动总数）。
    """
    since = int((time.time() - days * 24 * 3600) * 1000)
    stats = await Database.get().family_events_stats(since)
    return ApiResponse(data=stats)


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
        raise AppException("周报服务未就绪", code="report_unavailable", http_status=503)
    try:
        result = await svc.generate()
        return ApiResponse(data=result)
    except Exception as e:  # noqa: BLE001
        logger.exception("manual weekly report generation failed")
        raise AppException(f"生成失败: {e}", code="report_generate_failed", http_status=500)
