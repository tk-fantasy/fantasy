"""定时任务路由 — CRUD + 手动触发。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..schema.api_schemas import (
    ScheduledTaskCreateRequest,
    ScheduledTaskEnabledRequest,
    ScheduleParseRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/scheduled-tasks/parse-schedule")
async def parse_schedule(payload: ScheduleParseRequest) -> ApiResponse[dict]:
    """自然语言 → 触发配置。用 chat LLM 把短语翻译成 at/every/cron。"""
    from ..services.schedule_parser_service import parse_schedule as _parse

    try:
        result = await _parse(payload.phrase)
        return ApiResponse(data=result)
    except (ValueError, RuntimeError) as e:
        return ApiResponse(success=False, message=str(e), data=None)


@router.get("/scheduled-tasks")
async def list_scheduled_tasks(
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    svc = container.scheduler_service
    if svc is None:
        return ApiResponse(success=False, message="调度器未就绪", data=None)
    return ApiResponse(data=await svc.list_tasks())


@router.post("/scheduled-tasks")
async def create_scheduled_task(
    payload: ScheduledTaskCreateRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    svc = container.scheduler_service
    if svc is None:
        return ApiResponse(success=False, message="调度器未就绪", data=None)

    # name 为空时自动生成：schedule 摘要 + payload 摘要
    name = payload.name.strip()
    if not name:
        from ..services.scheduler_service import summarize_schedule
        sched_desc = summarize_schedule(payload.schedule)
        # payload 摘要：tool → 动作描述，message → 消息内容
        pl = payload.payload or {}
        if pl.get("kind") == "message":
            pl_desc = pl.get("message", "")[:20]
        elif pl.get("kind") == "tool":
            pl_desc = pl.get("tool_name", "")[:30]
        else:
            pl_desc = str(pl.get("kind", ""))[:20]
        name = f"{sched_desc} · {pl_desc}" if pl_desc else sched_desc

    # 记录创建者 user_id：执行时按它解析 per-user 模型，避免回退全局 agent
    # （全局 agent 的 httpx 客户端会被 per-user 构建误关，导致 Connection error）
    task = await svc.add_task({
        "name": name,
        "schedule": payload.schedule,
        "payload": payload.payload,
        "enabled": payload.enabled,
        "user_id": current_user["user_id"],
    })
    return ApiResponse(data=task)


@router.post("/scheduled-tasks/{task_id}/enabled")
async def set_scheduled_task_enabled(
    task_id: str,
    payload: ScheduledTaskEnabledRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    svc = container.scheduler_service
    if svc is None:
        return ApiResponse(success=False, message="调度器未就绪", data=None)
    task = await svc.set_enabled(task_id, payload.enabled)
    if task is None:
        return ApiResponse(success=False, message="任务不存在", data=None)
    return ApiResponse(data=task)


@router.post("/scheduled-tasks/{task_id}/run")
async def run_scheduled_task_now(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """手动触发一次（不等 schedule）。用于调试。"""
    svc = container.scheduler_service
    if svc is None:
        return ApiResponse(success=False, message="调度器未就绪", data=None)
    task = await svc.run_now(task_id)
    if task is None:
        return ApiResponse(success=False, message="任务不存在", data=None)
    return ApiResponse(data=task)


@router.delete("/scheduled-tasks/{task_id}")
async def delete_scheduled_task(
    task_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    svc = container.scheduler_service
    if svc is None:
        return ApiResponse(success=False, message="调度器未就绪", data=None)
    await svc.delete_task(task_id)
    return ApiResponse(data={"id": task_id})
