"""宿主侧集成通用机制扩展测试：UI 贡献 + method 桥接（不硬编码插件语义）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.integration.integration_layer import IntegrationLayer


def _layer_with_host(tmp_path, host_info):
    """构造带指定宿主集成注册信息的 layer（plugin_dir 为空目录 → 无 manifest 干扰）。"""
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer.host_integrations["feishu"] = host_info
    return layer


def _mock_container(layer):
    """构造带指定 integration_layer 的 mock container。"""
    c = MagicMock()
    c.integration_layer = layer
    return c


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Task 1：UI 贡献 ──

def test_list_ui_contributions_includes_host_integrations(tmp_path):
    """宿主集成的 ui_contributions 并入返回（形状与 manifest 贡献一致）。"""
    layer = _layer_with_host(tmp_path, {
        "name": "飞书机器人",
        "ui_contributions": [{"slot": "plugin_config_modal", "type": "custom_component"}],
    })
    assert layer.list_ui_contributions() == [{
        "plugin_id": "feishu", "slot": "plugin_config_modal",
        "type": "custom_component", "props": None,
        "state_key": None, "action": None,
    }]


def test_list_ui_contributions_host_without_contributions(tmp_path):
    """未声明 ui_contributions 的宿主集成不产生条目。"""
    layer = _layer_with_host(tmp_path, {"name": "some_host"})
    assert layer.list_ui_contributions() == []


# ── Task 2：method 桥接 + config_changed 约定 ──

def _plugin_method_target():
    from app.routes.integration_routes import PluginMethodRequest, call_plugin_method
    return PluginMethodRequest, call_plugin_method


def test_call_plugin_method_host_dispatch():
    """宿主集成携带 call_method 时直接分发，成功结果包 success/data 信封。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"status": "pending"})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    result = _run(call_plugin_method(
        "feishu", "qr_poll", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result == {"success": True, "data": {"status": "pending"}}
    call_method.assert_awaited_once_with("qr_poll", {})


def test_call_plugin_method_host_sync_call_method():
    """call_method 为同步函数时同样支持。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = MagicMock(return_value={"ok": 1})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    result = _run(call_plugin_method(
        "feishu", "anything", PluginMethodRequest(params={"a": 1}),
        container=_mock_container(layer=layer)))
    assert result == {"success": True, "data": {"ok": 1}}
    call_method.assert_called_once_with("anything", {"a": 1})


def test_call_plugin_method_host_failure_passthrough():
    """插件自述失败（success=False）原样透传，不包信封。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"success": False, "message": "未知方法: x"})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    result = _run(call_plugin_method(
        "feishu", "x", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result == {"success": False, "message": "未知方法: x"}


def test_call_plugin_method_config_changed_triggers_restart():
    """config_changed=True → 调 restart_host_integration_fn，applied=restarted。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"status": "success", "config_changed": True})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    container = _mock_container(layer=layer)
    container.restart_host_integration_fn = MagicMock(return_value=True)
    result = _run(call_plugin_method(
        "feishu", "qr_poll", PluginMethodRequest(params={}),
        container=container))
    assert result["data"]["applied"] == "restarted"
    container.restart_host_integration_fn.assert_called_once()
    assert container.restart_host_integration_fn.call_args.args[0] == "feishu"


def test_call_plugin_method_config_changed_restart_raises():
    """热重启异常时配置仍已保存：applied=saved，不向调用方抛错。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    call_method = AsyncMock(return_value={"status": "success", "config_changed": True})
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": call_method}}
    container = _mock_container(layer=layer)
    container.restart_host_integration_fn = MagicMock(side_effect=RuntimeError("boom"))
    result = _run(call_plugin_method(
        "feishu", "qr_poll", PluginMethodRequest(params={}),
        container=container))
    assert result["data"]["applied"] == "saved"


def test_call_plugin_method_framework_blocked_for_host():
    """框架方法黑名单对宿主集成同样生效。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    layer = MagicMock()
    layer.host_integrations = {"feishu": {"call_method": AsyncMock()}}
    result = _run(call_plugin_method(
        "feishu", "sink.speak", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result["success"] is False
    assert "框架方法" in result["message"]


def test_call_plugin_method_falls_back_to_supervisor():
    """host_integrations 无此插件时回落子进程路径（未运行报错，行为不回归）。"""
    PluginMethodRequest, call_plugin_method = _plugin_method_target()
    layer = MagicMock()
    layer.host_integrations = {}
    layer._supervisor.get_process.return_value = None
    result = _run(call_plugin_method(
        "xiaoai", "some_method", PluginMethodRequest(params={}),
        container=_mock_container(layer=layer)))
    assert result["success"] is False
    assert "未运行" in result["message"]


def test_start_host_integrations_registers_unconfigured(tmp_path):
    """start() 返回 None（凭证未配置）的宿主集成也收录并注册（alive=False）。

    这是扫码一键接入的前置：未配置时管理页才有卡片可点、restart 才找得到。
    注意：本测试 import app.main（模块级初始化在测试环境可承受，见 conftest 注释）。
    """
    plug = tmp_path / "dummyhost"
    plug.mkdir()
    (plug / "main.py").write_text(
        "def start(dispatch_fn, loop):\n    return None\n", encoding="utf-8")
    from app import main as app_main
    container = MagicMock()
    container.integration_layer = MagicMock()
    started = app_main._start_host_integrations(
        container, asyncio.new_event_loop(), integrations_dir=str(tmp_path))
    assert [n for n, _, _ in started if n == "dummyhost"]
    registered = {
        name: meta for name, meta in
        (c.args for c in container.integration_layer.register_host_integration.call_args_list)
    }
    assert registered["dummyhost"]["alive"] is False
    assert registered["dummyhost"]["call_method"] is None  # 未声明 → None
