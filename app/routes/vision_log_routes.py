"""识别日志路由：VL 识别留痕查询（测试插件面板 / 运维诊断用）+ 服务器目录浏览。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from ..core.api_models import ApiResponse
from ..core.auth import get_current_admin
from ..core.database import Database
from ..core.exceptions import AppException

router = APIRouter()


@router.get("/vision-logs")
async def list_vision_logs(
    camera_id: str = "",
    kind: str = "",
    limit: int = 100,
    admin: dict = Depends(get_current_admin),
):
    """最近识别日志（新在前）。kind: preview(预览分类) / rule_eval(规则判定) / action(动作执行/演练)。"""
    db = Database.get()
    rows = await db.vision_logs_tail(camera_id=camera_id, kind=kind, limit=limit)
    return ApiResponse(data=rows)


@router.delete("/vision-logs")
async def clear_vision_logs(
    camera_id: str = "",
    admin: dict = Depends(get_current_admin),
):
    """清空识别日志（可按 camera_id 过滤）。"""
    db = Database.get()
    deleted = await db.vision_logs_delete_camera(camera_id)
    return ApiResponse(data={"deleted": deleted})


# —— 服务器目录浏览（测试插件面板的文件选择器）——
# 只读列目录 + 按扩展名过滤，管理员鉴权。供"从本地选视频"的前端选择器用
# （浏览器安全模型拿不到本地绝对路径，必须由服务端列目录来选）。

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg"}
_DIR_ENTRY_LIMIT = 500

