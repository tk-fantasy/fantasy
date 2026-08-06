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


def test_toggle_broadcast_flips_state():
    """toggle 路由应翻转 sink_manager.broadcast_enabled 并调用 set_broadcast_enabled。"""
    mock_layer = MagicMock()
    mock_layer.sink_manager.broadcast_enabled = True
    mock_layer.set_broadcast_enabled = MagicMock()
    from app.routes.integration_routes import toggle_broadcast
    result = asyncio.new_event_loop().run_until_complete(
        toggle_broadcast(container=_mock_container(layer=mock_layer))
    )
    # True → False（翻转）
    assert result["success"] is True
    assert result["data"]["broadcast_enabled"] is False
    mock_layer.set_broadcast_enabled.assert_called_once_with(False)


def test_toggle_broadcast_when_disabled_returns_error():
    """集成平台未启用时，toggle 返回失败。"""
    from app.routes.integration_routes import toggle_broadcast
    result = asyncio.new_event_loop().run_until_complete(
        toggle_broadcast(container=_mock_container(layer=None))
    )
    assert result["success"] is False


def test_list_integrations_includes_broadcast_enabled_state():
    """list 返回里应含 broadcast_enabled 字段（前端读取用）。"""
    mock_layer = MagicMock()
    mock_layer.list_plugins.return_value = []
    mock_layer.sink_manager.broadcast_enabled = False
    from app.routes.integration_routes import list_integrations
    result = asyncio.new_event_loop().run_until_complete(
        list_integrations(container=_mock_container(layer=mock_layer))
    )
    assert result["data"]["broadcast_enabled"] is False


# ── UI 贡献机制测试 ──

def test_list_ui_contributions_returns_empty_when_no_layer():
    """无集成平台时返回空列表（前端无 UI 元素）。"""
    from app.routes.integration_routes import list_ui_contributions
    result = asyncio.new_event_loop().run_until_complete(
        list_ui_contributions(container=_mock_container(layer=None))
    )
    assert result["success"] is True
    assert result["data"] == []


def test_list_ui_contributions_returns_plugins_contributions():
    """返回所有插件的 ui_contribution（带 plugin_id）。"""
    mock_layer = MagicMock()
    mock_layer.list_ui_contributions.return_value = [
        {"plugin_id": "xiaoai", "slot": "chat_input_toolbar",
         "type": "toggle_button", "props": {"icon_on": "🔊"},
         "state_key": "broadcast_enabled", "action": "toggle_broadcast"}
    ]
    from app.routes.integration_routes import list_ui_contributions
    result = asyncio.new_event_loop().run_until_complete(
        list_ui_contributions(container=_mock_container(layer=mock_layer))
    )
    assert len(result["data"]) == 1
    assert result["data"][0]["plugin_id"] == "xiaoai"
    assert result["data"][0]["type"] == "toggle_button"


def test_get_state_reads_broadcast_enabled():
    """GET /state/broadcast_enabled 读 sink_manager.broadcast_enabled。"""
    mock_layer = MagicMock()
    mock_layer.sink_manager.broadcast_enabled = True
    from app.routes.integration_routes import get_state
    result = asyncio.new_event_loop().run_until_complete(
        get_state("broadcast_enabled", container=_mock_container(layer=mock_layer))
    )
    assert result["success"] is True
    assert result["data"]["value"] is True


def test_get_state_unknown_key_returns_error():
    """未注册的 state_key 返回失败（安全边界）。"""
    mock_layer = MagicMock()
    from app.routes.integration_routes import get_state
    result = asyncio.new_event_loop().run_until_complete(
        get_state("nonexistent_key", container=_mock_container(layer=mock_layer))
    )
    assert result["success"] is False


def test_invoke_action_toggle_broadcast():
    """POST /action/toggle_broadcast 切换广播开关。"""
    mock_layer = MagicMock()
    mock_layer.sink_manager.broadcast_enabled = True
    mock_layer.set_broadcast_enabled = MagicMock()
    from app.routes.integration_routes import invoke_action
    result = asyncio.new_event_loop().run_until_complete(
        invoke_action("toggle_broadcast", container=_mock_container(layer=mock_layer))
    )
    assert result["success"] is True
    assert result["data"]["broadcast_enabled"] is False  # True → False
    mock_layer.set_broadcast_enabled.assert_called_once_with(False)


def test_invoke_action_unknown_returns_error():
    """未注册的 action 返回失败（安全边界）。"""
    mock_layer = MagicMock()
    from app.routes.integration_routes import invoke_action
    result = asyncio.new_event_loop().run_until_complete(
        invoke_action("nonexistent_action", container=_mock_container(layer=mock_layer))
    )
    assert result["success"] is False


def test_get_state_no_layer_returns_error():
    """无集成平台时 state 返回失败。"""
    from app.routes.integration_routes import get_state
    result = asyncio.new_event_loop().run_until_complete(
        get_state("broadcast_enabled", container=_mock_container(layer=None))
    )
    assert result["success"] is False
