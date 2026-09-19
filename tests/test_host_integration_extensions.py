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
