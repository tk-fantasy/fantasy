"""自动化配置路由 — 定时器兜底开关/间隔、默认冷却、/camera 视觉展示开关。

dhash 事件触发 + 3s 节流 + 设备状态门控（P1）是评估核心，本路由只管用户可调
的运行时开关与默认值，不涉及评估逻辑本身。砍掉了原计划的 /semantic-cache*
端点（语义缓存 P2 整段砍除）；dhash 阈值调节端点留 P1（与摄像头共享
vision.motion_threshold）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section
from ..schema.api_schemas import AutomationSilentRequest, AutomationVisionRecognizerRequest, AutomationCooldownRequest, AutomationDhashThresholdRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/automation/status")
async def automation_status(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """所有自动化配置 + 运行时状态（供前端「自动化」modal 与 /camera 视觉开关）。"""
    agent = container.automation_agent_ref[0]
    motion_hash_size = int(get_config("vision.motion_hash_size", 16))
    return ApiResponse(data={
        "silent_eval_enabled": bool(get_config("automation.silent_eval_enabled", True)),
        "silent_eval_interval_seconds": int(get_config("automation.silent_eval_interval_seconds", 60)),
        "default_cooldown_seconds": int(get_config("automation.default_cooldown_seconds", 5)),
        # dhash 阈值复用 vision.motion_threshold（P1 滑块用），范围 1~hash_size²
        "motion_threshold": int(get_config("vision.motion_threshold", 15)),
        "motion_hash_size": motion_hash_size,
        "motion_threshold_max": motion_hash_size * motion_hash_size,
        "min_trigger_interval": float(get_config("vision.min_infer_interval_seconds", 3.0)),
        "camera_vl_display_enabled": bool(get_config("automation.camera_vl_display_enabled", True)),
        "running": bool(agent._running) if agent else False,
        "eval_count": int(agent._eval_count) if agent else 0,
    })


@router.post("/automation/silent")
async def set_silent_eval(
    payload: AutomationSilentRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """热切换定时器兜底（静默推理）：开关 + 间隔。

    间隔走 set_silent_interval（带 0.5s 防抖，松手后生效一次并立刻评估一次）。
    开关走 set_silent_enabled（call_soon_threadsafe，可跨线程）。配置同步落盘
    config.json，重启后保持。
    """
    agent = container.automation_agent_ref[0]
    if agent is None:
        return ApiResponse(code="not_started", message="AutomationAgent 未启动", data={"saved": False})
    config_patch: dict = {}
    if payload.interval_seconds is not None:
        # 5~3600s 钳制（与前端滑块范围一致）
        interval = max(5, min(3600, int(payload.interval_seconds)))
        agent.set_silent_interval(float(interval))
        config_patch["silent_eval_interval_seconds"] = interval
    if payload.enabled is not None:
        agent.set_silent_enabled(bool(payload.enabled))
        config_patch["silent_eval_enabled"] = bool(payload.enabled)
    if config_patch:
        update_config_section("automation", config_patch)
        logger.info("Automation silent config updated: %s", config_patch)
    return ApiResponse(data={"saved": True, **config_patch})


@router.post("/automation/vision-recognizer")
async def set_vision_recognizer(
    payload: AutomationVisionRecognizerRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """开关 /camera 页面的视觉展示推理（classify_frame 预览）。

    关掉只停 /camera 预览，dhash 运动检测与自动化触发不受影响。配置落盘，
    重启后保持。
    """
    camera_manager = container.camera_manager
    if camera_manager is None:
        return ApiResponse(code="not_started", message="摄像头未启动", data={"saved": False})
    enabled = bool(payload.enabled)
    camera_manager.set_camera_vl_display_enabled(enabled)
    update_config_section("automation", {"camera_vl_display_enabled": enabled})
    logger.info("Camera VL display %s", "enabled" if enabled else "disabled")
    return ApiResponse(data={"saved": True, "camera_vl_display_enabled": enabled})


@router.post("/automation/cooldown")
async def set_default_cooldown(payload: AutomationCooldownRequest) -> ApiResponse[dict]:
    """设置规则默认冷却秒数。

    只影响**新建/无显式 cooldown 的规则**：老规则已各自持久化
    cooldown_seconds，不受此默认值变更影响。automation_service / rule_service
    评估与建规则时通过 get_config 实时读取，故落盘 config 即可，无需热推送。
    """
    cooldown = max(1, min(3600, int(payload.cooldown_seconds)))
    update_config_section("automation", {"default_cooldown_seconds": cooldown})
    logger.info("Default cooldown updated: %ds (new rules only)", cooldown)
    return ApiResponse(data={"saved": True, "default_cooldown_seconds": cooldown})


@router.post("/automation/dhash-threshold")
async def set_dhash_threshold(
    payload: AutomationDhashThresholdRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """调整 dhash 运动判定阈值（复用 vision.motion_threshold，与摄像头预览共享）。

    滑块范围 1~hash_size²；拉到最大（=hash_size²）时 distance > threshold 永不成立，
    dhash 不触发，自动化降级为纯定时器兜底。热更新运行中的 MotionDetector + 落盘
    config（重启后保持）。
    """
    camera_manager = container.camera_manager
    max_threshold = int(get_config("vision.motion_hash_size", 16)) ** 2
    threshold = max(1, min(max_threshold, int(payload.threshold)))
    if camera_manager is not None:
        camera_manager.set_motion_threshold(threshold)
    update_config_section("vision", {"motion_threshold": threshold})
    logger.info("dhash motion threshold updated: %d (max=%d)", threshold, max_threshold)
    return ApiResponse(data={"saved": True, "motion_threshold": threshold, "motion_threshold_max": max_threshold})
