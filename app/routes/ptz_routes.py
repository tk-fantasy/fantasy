"""PTZ 云台路由 — 方向控制、停止、步进、配置。

onvif-zeep-async 4.x 的 ONVIFCamera 是 async API，ptz_service 全 async，
路由层直接 await 即可，不需要 to_thread。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section, write_secrets
from ..schema.api_schemas import PtzConfigRequest, PtzMoveRequest, PtzStepRequest
from ..services.ptz_service import ptz_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ptz/status")
async def ptz_status() -> ApiResponse[dict]:
    """PTZ 是否启用 + 单步时长。前端打开摄像头面板时拉一次，
    决定是否显示云台控件，并用 step_ms 做点击冷却。"""
    enabled = bool(get_config("ptz.enabled", False))
    step_ms = int(get_config("ptz.step_ms", 300))
    return ApiResponse(data={"enabled": enabled, "step_ms": step_ms})


@router.get("/ptz/config")
async def ptz_config_get() -> ApiResponse[dict]:
    """获取 PTZ 配置。密码不回传明文，只回 has_password 标志。"""
    cfg = dict(get_config("ptz", {}))
    pwd_env = str(cfg.get("password_env", "")).strip()
    cfg["has_password"] = bool(pwd_env and os.getenv(pwd_env))
    return ApiResponse(data=cfg)


@router.post("/ptz/config")
async def ptz_config_set(payload: PtzConfigRequest) -> ApiResponse[dict]:
    """保存 PTZ 配置。密码单独写 .env（PTZ_PASSWORD），config.json 只存变量名。"""
    config_data = {
        "enabled": payload.enabled,
        "ip": payload.ip,
        "port": payload.port,
        "username": payload.username,
        "speed": payload.speed,
        "step_ms": payload.step_ms,
    }
    update_config_section("ptz", config_data)
    if payload.password:
        write_secrets({"PTZ_PASSWORD": payload.password})
        # 确保 config.json 记住变量名（_ensure_connected 靠它取密码）
        update_config_section("ptz", {"password_env": "PTZ_PASSWORD"})
        logger.info("PTZ password updated in .env")
    return ApiResponse(data={"saved": True})


@router.post("/ptz/move")
async def ptz_move(payload: PtzMoveRequest) -> ApiResponse[dict]:
    """开始持续转动（按住式）。前端松开时调 /ptz/stop。"""
    result = await ptz_service.move(payload.direction)
    return ApiResponse(data=result)


@router.post("/ptz/stop")
async def ptz_stop() -> ApiResponse[dict]:
    """停止转动（松开 / 紧急停转）。"""
    result = await ptz_service.stop()
    return ApiResponse(data=result)


@router.post("/ptz/step")
async def ptz_step(payload: PtzStepRequest) -> ApiResponse[dict]:
    """步进（点按式）：点一下转一小段后自动停。停转由后端保证。"""
    duration = int(get_config("ptz.step_ms", 300))
    result = await ptz_service.step(payload.direction, duration)
    return ApiResponse(data=result)
