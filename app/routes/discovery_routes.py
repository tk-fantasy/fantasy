"""ONVIF 摄像头发现路由 — 手动触发发现 + 手动填 IP 兜底。

discovery_service 是无状态单例,路由直接 import 用。
手动填 IP 时用 probe_ptz 验证凭证 + 写 config(同 advanced_routes 的模式)。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section
from ..services.camera_discovery_service import discovery_service
from ..services.config_probes import probe_ptz

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/discovery/status")
async def get_discovery_status() -> ApiResponse[dict]:
    """查询发现服务当前状态(idle/scanning/found/not_found/error)。"""
    return ApiResponse(data=discovery_service.status)


@router.post("/discovery/find")
async def trigger_discovery() -> ApiResponse[dict]:
    """手动触发一次 ONVIF 发现。找到则自动更新 config 并返回新 IP。

    供前端「重新发现摄像头」按钮调用。超时由 config discovery_timeout_seconds 控制。
    """
    if not bool(get_config("vision.discovery_enabled", False)):
        return ApiResponse(
            code="disabled",
            message="自动发现未启用(在配置里开启 discovery_enabled)",
            data={"found": False},
        )
    found_ip = await discovery_service.find_and_apply()
    return ApiResponse(
        code="ok" if found_ip else "not_found",
        message=f"已发现并更新摄像头 IP: {found_ip}" if found_ip else discovery_service.status.get("last_error", "未找到设备"),
        data={"found": bool(found_ip), "ip": found_ip, **discovery_service.status},
    )


