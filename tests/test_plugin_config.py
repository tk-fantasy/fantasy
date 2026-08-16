"""插件配置管理页（GET/POST /integrations/{id}/config）+ 飞书凭证优先级测试。

覆盖：
- config_helper：host_configs 读写、secret 留空保持原值合并
- 路由：GET 脱敏回显、POST 必填校验、宿主集成走热重启回调、写审计
- 飞书 _read_config：界面配置逐字段优先于 .env，来源标记
- SDK：AETHER_PLUGIN_CONFIG 覆盖 manifest config_schema 默认值
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_container(layer):
    c = MagicMock()
    c.integration_layer = layer
    return c


def _layer_with_plugins(plugins, host_ids=()):
    layer = MagicMock()
    layer.list_plugins.return_value = plugins
    layer.host_integrations = {i: {} for i in host_ids}
    layer.restart_subprocess_plugin = AsyncMock(return_value=True)
    return layer


FEISHU_PLUGIN = {
    "id": "feishu", "name": "飞书机器人", "version": "1.0.1",
    "description": "", "capabilities": ["host_integration"],
    "alive": True, "enabled": True,
    "config_schema": {
        "app_id": {"type": "string", "required": True, "label": "App ID"},
        "app_secret": {"type": "secret", "required": True, "label": "App Secret"},
        "encrypt_key": {"type": "secret", "required": False, "label": "Encrypt Key"},
    },
    "has_config_set": False,
}


# ==================== config_helper ====================

class TestHostConfigStore:
    def test_roundtrip_and_deep_merge(self, tmp_path, monkeypatch):
        from app.integration import config_helper as ch
        calls = {}
        monkeypatch.setattr(ch, "get_config",
                            lambda path, default=None: calls.get(path, default))
        monkeypatch.setattr(ch, "update_config_section",
                            lambda section, values: calls.update({section: values}))

        ch.set_host_config("feishu", {"app_id": "cli_1"})
        assert calls["integration"] == {"host_configs": {"feishu": {"app_id": "cli_1"}}}

    def test_merge_secret_blank_keeps_old(self, monkeypatch):
        from app.integration import config_helper as ch
        stored = {"app_id": "cli_1", "app_secret": "sec_ret_value"}
        saved = {}
        monkeypatch.setattr(ch, "get_config",
                            lambda path, default=None: stored if path.endswith("feishu") else default)
        monkeypatch.setattr(ch, "set_host_config",
                            lambda pid, values: saved.update(values))

        merged = ch.merge_plugin_config(
            "feishu",
            {"app_id": "cli_2", "app_secret": "  ", "encrypt_key": "ek"},  # secret 留空
            secret_keys={"app_secret", "encrypt_key"},
        )
        assert merged["app_id"] == "cli_2"          # 非密钥字段覆盖
        assert merged["app_secret"] == "sec_ret_value"  # 留空保持原值
        assert merged["encrypt_key"] == "ek"         # 新填的密钥写入
        assert saved == merged                       # 已持久化


# ==================== 路由 ====================

class TestGetPluginConfig:
    def test_secret_masked_not_returned(self, monkeypatch):
        from app.integration import config_helper as ch
        from app.routes.integration_routes import get_plugin_config
        monkeypatch.setattr(ch, "get_config", lambda path, default=None: (
            {"app_id": "cli_aaaabbbb", "app_secret": "secret_value_12345"}
            if path.endswith("feishu") else default
        ))
        layer = _layer_with_plugins([FEISHU_PLUGIN], host_ids=("feishu",))
        result = asyncio.run(get_plugin_config("feishu", container=_mock_container(layer)))
        values = result["data"]["values"]
        assert values["app_id"] == "cli_aaaabbbb"                    # 明文字段回显
        assert "secret_value_12345" not in str(values)               # 密钥不回明文
        assert values["app_secret"]["is_set"] is True
        assert values["app_secret"]["masked"].startswith("secr")

    def test_unknown_plugin(self):
        from app.routes.integration_routes import get_plugin_config
        layer = _layer_with_plugins([FEISHU_PLUGIN])
        result = asyncio.run(get_plugin_config("nope", container=_mock_container(layer)))
        assert result["success"] is False


class TestSavePluginConfig:
    def _save(self, layer, values, restart_fn=None, plugin_id="feishu"):
        from app.routes.integration_routes import PluginConfigRequest, save_plugin_config
        c = _mock_container(layer)
        c.restart_host_integration_fn = restart_fn or (lambda name, loop=None: True)
        return asyncio.run(save_plugin_config(
            plugin_id, PluginConfigRequest(values=values), container=c,
            admin={"username": "op", "user_id": "u1"},
        ))

    def test_host_integration_hot_restart_and_audit(self, monkeypatch):
        from app.integration import config_helper as ch
        from app.ops import audit
        recorded = []
        monkeypatch.setattr(ch, "get_config", lambda path, default=None: default)
        monkeypatch.setattr(ch, "set_host_config", lambda pid, values: None)
        monkeypatch.setattr(audit, "record",
                            lambda operator, action, detail=None: recorded.append((operator, action, detail)) or {})

        layer = _layer_with_plugins([FEISHU_PLUGIN], host_ids=("feishu",))
        restarted = []
        result = self._save(layer, {"app_id": "cli_x", "app_secret": "sk_1"},
                            restart_fn=lambda name, loop=None: restarted.append(name) or True)
        assert result["success"] is True
        assert result["data"]["applied"] == "restarted"
        assert restarted == ["feishu"]
        assert recorded and recorded[0][0] == "op" and recorded[0][1] == "plugin_config"

    def test_required_missing_rejected(self, monkeypatch):
        from app.integration import config_helper as ch
        monkeypatch.setattr(ch, "get_config", lambda path, default=None: default)
        monkeypatch.setattr(ch, "set_host_config", lambda pid, values: None)
        layer = _layer_with_plugins([FEISHU_PLUGIN], host_ids=("feishu",))
        result = self._save(layer, {"app_id": "cli_x"})  # 缺 app_secret
        assert result["success"] is False
        assert "app_secret" in result["message"]

    def test_unknown_fields_dropped(self, monkeypatch):
        from app.integration import config_helper as ch
        captured = {}
        monkeypatch.setattr(ch, "get_config", lambda path, default=None: default)
        monkeypatch.setattr(ch, "merge_plugin_config",
                            lambda pid, updates, secret_keys: captured.update(updates) or dict(updates))
        layer = _layer_with_plugins([FEISHU_PLUGIN], host_ids=("feishu",))
        self._save(layer, {"app_id": "cli_x", "hack": "1"})
        assert "hack" not in captured

    def test_subprocess_plugin_restart_path(self, monkeypatch):
        from app.integration import config_helper as ch
        monkeypatch.setattr(ch, "get_config",
                            lambda path, default=None: {"app_id": "cli_x", "app_secret": "s"} if path.endswith("xiaoai") else default)
        monkeypatch.setattr(ch, "set_host_config", lambda pid, values: None)
        plugin = dict(FEISHU_PLUGIN, id="xiaoai",
                      config_schema={"entity_id": {"type": "string", "required": True}})
        layer = _layer_with_plugins([plugin])  # 非宿主集成 → 子进程路径
        result = self._save(layer, {"entity_id": "media_player.x"}, plugin_id="xiaoai")
        assert result["data"]["applied"] == "restarted"
        layer.restart_subprocess_plugin.assert_awaited_once_with("xiaoai")


# ==================== 飞书凭证优先级 ====================

class TestFeishuReadConfig:
    def test_ui_overrides_env_per_field(self, monkeypatch):
        from integrations.feishu.main import _read_config
        monkeypatch.setenv("FEISHU_APP_ID", "cli_env")
        monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret")
        monkeypatch.delenv("FEISHU_VERIFICATION_TOKEN", raising=False)
        with patch("app.integration.config_helper.get_host_config",
                   return_value={"app_id": "cli_ui"}):
            cfg, source = _read_config()
        assert cfg["app_id"] == "cli_ui"            # 界面优先
        assert cfg["app_secret"] == "env_secret"    # 界面没填的回退 env
        assert cfg["verification_token"] == ""
        assert source == "ui"

    def test_env_only_source(self, monkeypatch):
        from integrations.feishu.main import _read_config
        monkeypatch.setenv("FEISHU_APP_ID", "cli_env")
        monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret")
        with patch("app.integration.config_helper.get_host_config", return_value={}):
            cfg, source = _read_config()
        assert cfg["app_id"] == "cli_env"
        assert source == "env"


# ==================== SDK 配置覆盖 ====================

class TestSdkConfigOverlay:
    @pytest.mark.asyncio
    async def test_env_config_overrides_schema_defaults(self, tmp_path, monkeypatch):
        """AETHER_PLUGIN_CONFIG 覆盖 config_schema default，插件 setup 读到新值。"""
        from app.integration.sdk import stdio_runtime

        manifest = {
            "id": "p1",
            "capabilities": [{
                "type": "output_sink", "id": "s1",
                "config_schema": {
                    "entity_id": {"type": "string", "default": "old_entity"},
                    "mode": {"type": "enum", "options": ["a", "b"], "default": "a"},
                },
            }],
        }
        mf = tmp_path / "manifest.json"
        mf.write_text(__import__("json").dumps(manifest), encoding="utf-8")

        monkeypatch.setenv("AETHER_PLUGIN_CONFIG",
                           __import__("json").dumps({"entity_id": "new_entity"}))

        captured = {}

        class FakePlugin:
            def setup(self, m):
                cap = m["capabilities"][0]
                captured["entity_id"] = cap["config_schema"]["entity_id"]["default"]
                captured["mode"] = cap["config_schema"]["mode"]["default"]

        runtime = MagicMock()
        runtime.run = AsyncMock()
        with patch.object(stdio_runtime, "_StdioRuntime", return_value=runtime):
            await stdio_runtime.run_stdio_plugin(FakePlugin, str(mf))

        assert captured["entity_id"] == "new_entity"   # 被覆盖
        assert captured["mode"] == "a"                 # 未配置的保持默认
