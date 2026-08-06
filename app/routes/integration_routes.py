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
    """列出所有集成插件及其运行状态（含禁用态）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": {"plugins": [], "enabled": False,
                                          "broadcast_enabled": False}}
    return {"success": True, "data": {
        "plugins": layer.list_plugins(),
        "enabled": True,
        "broadcast_enabled": layer.sink_manager.broadcast_enabled,
    }}


@router.post("/integrations/{plugin_id}/toggle-enabled")
async def toggle_plugin_enabled(plugin_id: str, container=Depends(get_container)):
    """切换插件启用/禁用（持久化，需重启生效）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    # 查当前状态
    plugins = layer.list_plugins()
    target = next((p for p in plugins if p["id"] == plugin_id), None)
    if target is None:
        return {"success": False, "message": f"未知插件: {plugin_id}"}
    new_enabled = not target["enabled"]
    layer.set_plugin_enabled(plugin_id, new_enabled)
    return {"success": True, "data": {"id": plugin_id, "enabled": new_enabled}}


@router.post("/integrations/broadcast/toggle")
async def toggle_broadcast(container=Depends(get_container)):
    """切换全局广播开关（开↔关），持久化到 config，立即生效。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    new_state = not layer.sink_manager.broadcast_enabled
    layer.set_broadcast_enabled(new_state)
    return {"success": True, "data": {"broadcast_enabled": new_state}}


@router.post("/integrations/broadcast")
async def manual_broadcast(req: BroadcastRequest, container=Depends(get_container)):
    """手动触发一次广播（测试/调试用）。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    await layer.sink_manager.broadcast(req.text, req.msg_id)
    return {"success": True}


# ── UI 贡献机制：插件声明 UI，Aether 通用路由读状态/触发动作 ──

@router.get("/integrations/ui_contributions")
async def list_ui_contributions(container=Depends(get_container)):
    """返回所有插件声明的 UI 贡献（前端通用渲染器据此渲染）。

    没插件 / 插件无 ui_contribution → 空列表 → 前端无该 UI 元素。
    """
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": []}
    return {"success": True, "data": layer.list_ui_contributions()}


# state_key → 读取函数注册表（框架能力，不认得具体插件）
# 插件只能用已注册的 state_key——这是安全边界
STATE_HANDLERS = {
    "broadcast_enabled": lambda layer: layer.sink_manager.broadcast_enabled,
}


# action → 触发函数注册表（框架能力，不认得具体插件）
# 插件只能用已注册的 action——这是安全边界
async def _toggle_broadcast(layer):
    """切换全局广播开关（框架能力，非小爱专属）。"""
    new_state = not layer.sink_manager.broadcast_enabled
    layer.set_broadcast_enabled(new_state)
    return {"broadcast_enabled": new_state}


ACTION_HANDLERS = {
    "toggle_broadcast": _toggle_broadcast,
}


@router.get("/integrations/state/{state_key}")
async def get_state(state_key: str, container=Depends(get_container)):
    """通用状态读取路由。按 state_key 路由到框架能力。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    handler = STATE_HANDLERS.get(state_key)
    if handler is None:
        return {"success": False, "message": f"未知 state_key: {state_key}"}
    return {"success": True, "data": {"value": handler(layer)}}


@router.post("/integrations/action/{action}")
async def invoke_action(action: str, container=Depends(get_container)):
    """通用动作触发路由。按 action 路由到框架能力。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return {"success": False, "message": f"未知 action: {action}"}
    result = await handler(layer)
    return {"success": True, "data": result}
