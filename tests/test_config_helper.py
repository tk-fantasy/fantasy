"""config_helper 测试（禁用插件列表读写）。"""

from unittest.mock import MagicMock, patch

from app.integration import config_helper


def test_get_disabled_plugins_defaults_empty(monkeypatch):
    monkeypatch.setattr(config_helper, "get_config", lambda path, default=None: default)
    assert config_helper.get_disabled_plugins() == []


def test_set_plugin_disabled_adds_to_list(monkeypatch):
    captured = {"current": ["existing"]}
    def fake_update(section, values):
        captured["current"] = values["disabled_plugins"]
    monkeypatch.setattr(config_helper, "get_config",
                        lambda path, default=None: captured["current"] if path == "integration.disabled_plugins" else default)
    monkeypatch.setattr(config_helper, "update_config_section", fake_update)
    result = config_helper.set_plugin_disabled("newone", True)
    assert "newone" in result
    assert "existing" in result


def test_set_plugin_enabled_removes_from_list(monkeypatch):
    captured = {"current": ["a", "b"]}
    def fake_update(section, values):
        captured["current"] = values["disabled_plugins"]
    monkeypatch.setattr(config_helper, "get_config",
                        lambda path, default=None: captured["current"] if path == "integration.disabled_plugins" else default)
    monkeypatch.setattr(config_helper, "update_config_section", fake_update)
    result = config_helper.set_plugin_disabled("a", False)  # 启用 a
    assert "a" not in result
    assert "b" in result
