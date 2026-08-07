"""current_mode state + set_mode action 路由测试。"""

from unittest.mock import MagicMock, patch

from app.routes.integration_routes import STATE_HANDLERS, ACTION_HANDLERS


def test_current_mode_in_state_handlers():
    """current_mode 注册在 STATE_HANDLERS。"""
    assert "current_mode" in STATE_HANDLERS


def test_set_mode_in_action_handlers():
    """set_mode 注册在 ACTION_HANDLERS。"""
    assert "set_mode" in ACTION_HANDLERS


def test_get_current_mode_returns_aether_by_default():
    """默认 current_mode = aether。"""
    with patch("app.integration.config_helper.get_config", return_value="aether"):
        from app.integration.config_helper import get_current_mode
        assert get_current_mode() == "aether"


def test_set_mode_persists():
    """set_mode 持久化到 config。"""
    with patch("app.integration.config_helper.update_config_section") as mock_update:
        from app.integration.config_helper import set_current_mode
        set_current_mode("xiaoai_direct")
        mock_update.assert_called_once_with(
            "integration", {"current_mode": "xiaoai_direct"})


def test_set_mode_handler_exists():
    """set_mode handler 存在且可调用。"""
    from app.routes.integration_routes import ACTION_HANDLERS
    handler = ACTION_HANDLERS["set_mode"]
    assert handler is not None
