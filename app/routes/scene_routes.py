"""场景模式路由 — 场景 CRUD / 捕获 / 应用。

家庭共享模型：全家成员都能创建/应用/删除场景（与其他设备功能一致）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.exceptions import AppException
from ..schema.api_schemas import SceneCreateRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _svc(container: AppContainer):
    if container.scene_service is None:
        raise AppException("场景服务未就绪", code="scene_unavailable", http_status=503)
    return container.scene_service


@router.get("/scenes")
async def list_scenes(container: AppContainer = Depends(get_container)) -> ApiResponse[list[dict]]:
    return ApiResponse(data=await _svc(container).list_scenes())


@router.post("/scenes")
async def create_scene(
    payload: SceneCreateRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """创建/更新场景。actions 为空且 capture=true 时从当前设备状态捕获。"""
    svc = _svc(container)
    try:
        if payload.capture:
            scene = await svc.capture_scene(payload.name, user_id=current_user["user_id"])
        else:
            scene = await svc.create_scene(
                payload.name, payload.actions or [],
                user_id=current_user["user_id"], scene_id=payload.id or "")
        return ApiResponse(data=scene)
    except ValueError as e:
        return ApiResponse(success=False, message=str(e), data=None)


@router.post("/scenes/{scene_id}/apply")
async def apply_scene(
    scene_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    try:
        result = await _svc(container).apply_scene(scene_id)
        return ApiResponse(data=result)
    except ValueError as e:
        return ApiResponse(success=False, message=str(e), data=None)
    except RuntimeError as e:
        return ApiResponse(success=False, message=str(e), data=None)


@router.delete("/scenes/{scene_id}")
async def delete_scene(
    scene_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    ok = await _svc(container).delete_scene(scene_id)
    return ApiResponse(data={"deleted": ok})
