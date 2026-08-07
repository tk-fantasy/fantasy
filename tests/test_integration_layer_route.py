"""IntegrationLayer.route_inbound 测试（通用，不硬编码插件）。"""

import asyncio

from app.integration.integration_layer import IntegrationLayer

INTEGRATIONS_TESTS_DIR = "tests/integrations"


def test_route_inbound_no_plugins_returns_error():
    """无 inbound_router 插件时返回 error。"""
    layer = IntegrationLayer(plugin_dir="nonexistent_dir")

    async def go():
        result = await layer.route_inbound("播放音乐", "some_mode")
        assert result["ok"] is False
        assert "no inbound router" in result["error"]

    asyncio.new_event_loop().run_until_complete(go())
