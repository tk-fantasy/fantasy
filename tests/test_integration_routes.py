"""集成平台路由测试。

不启动整个 app（避免 lifespan 重依赖），直接调路由函数 + 传 mock container 参数。
真实启动验证（插件 spawn）见 E2E 与 docs Phase 1 验收清单。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _mock_container(layer):
    """构造带指定 integration_layer 的 mock container。"""
    c = MagicMock()
    c.integration_layer = layer
    return c


def test_list_integrations_returns_200_when_disabled():
    """集成平台未启用（layer=None）时，返回 enabled=False + 空列表。"""
    from app.routes.integration_routes import list_integrations
    result = asyncio.new_event_loop().run_until_complete(
        list_integrations(container=_mock_container(layer=None))
    )
    assert result["success"] is True
    assert result["data"]["enabled"] is False
    assert result["data"]["plugins"] == []


def test_list_integrations_returns_plugins_when_enabled():
    """集成平台启用时，返回 layer.list_plugins() 的结果。"""
    mock_layer = MagicMock()
    mock_layer.list_plugins.return_value = [
        {"id": "xiaoai", "name": "小爱", "version": "1.0.0",
         "capabilities": ["output_sink"], "alive": True}
    ]
    from app.routes.integration_routes import list_integrations
    result = asyncio.new_event_loop().run_until_complete(
        list_integrations(container=_mock_container(layer=mock_layer))
    )
    assert result["success"] is True
    assert result["data"]["enabled"] is True
    assert len(result["data"]["plugins"]) == 1
    assert result["data"]["plugins"][0]["id"] == "xiaoai"


def test_manual_broadcast_calls_sink_manager():
    """手动广播应调用 layer.sink_manager.broadcast。"""
    mock_layer = MagicMock()
    mock_layer.sink_manager.broadcast = AsyncMock()
    from app.routes.integration_routes import BroadcastRequest, manual_broadcast
    req = BroadcastRequest(text="测试消息", msg_id="m1")
    result = asyncio.new_event_loop().run_until_complete(
        manual_broadcast(req, container=_mock_container(layer=mock_layer))
    )
    assert result["success"] is True
    mock_layer.sink_manager.broadcast.assert_awaited_once_with("测试消息", "m1")


def test_manual_broadcast_returns_error_when_disabled():
    """集成平台未启用时，手动广播返回失败。"""
    from app.routes.integration_routes import BroadcastRequest, manual_broadcast
    req = BroadcastRequest(text="测试消息")
    result = asyncio.new_event_loop().run_until_complete(
        manual_broadcast(req, container=_mock_container(layer=None))
    )
    assert result["success"] is False
