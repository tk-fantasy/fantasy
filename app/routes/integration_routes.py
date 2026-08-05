"""集成插件平台管理路由。"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..container import get_container

router = APIRouter()
logger = logging.getLogger(__name__)


class BroadcastRequest(BaseModel):
    """手动广播请求体（测试用）。"""
    text: str
    msg_id: str = "manual"


@router.get("/integrations")
async def list_integrations(container=Depends(get_container)):
    """列出所有集成插件及其运行状态。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": {"plugins": [], "enabled": False}}
    return {"success": True, "data": {"plugins": layer.list_plugins(), "enabled": True}}


@router.post("/integrations/broadcast")
async def manual_broadcast(req: BroadcastRequest, container=Depends(get_container)):
    """手动触发一次广播（测试/调试用）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    await layer.sink_manager.broadcast(req.text, req.msg_id)
    return {"success": True}
