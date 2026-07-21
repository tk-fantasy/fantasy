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
from ..services.config_probes import probe_ptz
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
    """保存 PTZ 配置。密码单独写 .env（PTZ_PASSWORD），config.json 只存变量名。

    保存前 probe：enabled=True 且 ip 非空时，用候选凭证连一次 ONVIF 设备，
    失败拒绝落盘。避免错 IP/凭证存进去后点云台才发现不工作。
    """
    # 先确定 probe 用的密码：用户填了新密码用它，否则从 .env 读现有的
    new_pwd = (payload.password or "").strip()
    probe_pwd = new_pwd or os.getenv("PTZ_PASSWORD", "")

    # enabled 且填了 ip 才 probe（disabled 或没 ip 时不验证）
    if payload.enabled and payload.ip:
        result = await probe_ptz(payload.ip, payload.port, payload.username, probe_pwd)
        if not result.ok:
            logger.warning("PTZ config save rejected: %s (%s)", result.reason, result.detail)
            return ApiResponse(
                code="probe_failed",
                message=result.detail,
                data={"saved": False, "section": "ptz", **result.to_dict()},
            )

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


@router.post("/ptz/test")
async def test_ptz_connection() -> ApiResponse[dict]:
    """测试 PTZ 连接（用当前已保存的 ip/port/username/password）。

    前端「测试连接」按钮调用。独立临时连接，不污染运行中的 ptz_service 单例。
    """
    ip = str(get_config("ptz.ip", "") or "").strip()
    if not ip:
        return ApiResponse(
            code="probe_failed",
            message="未配置 PTZ IP",
            data={"connected": False, "reason": "bad_format", "detail": "未配置 PTZ IP"},
        )
    port = int(get_config("ptz.port", 80))
    username = str(get_config("ptz.username", "") or "").strip()
    password = os.getenv("PTZ_PASSWORD", "")
    result = await probe_ptz(ip, port, username, password)
    if not result.ok:
        logger.warning("PTZ test failed: %s (%s)", result.reason, result.detail)
    return ApiResponse(
        code="probe_failed" if not result.ok else "ok",
        message=result.detail,
        data={"connected": result.ok, **result.to_dict()},
    )


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
