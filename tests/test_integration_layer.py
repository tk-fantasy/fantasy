"""IntegrationLayer 门面测试。"""

import asyncio

import pytest

from app.integration.integration_layer import IntegrationLayer

INTEGRATIONS_TESTS_DIR = "tests/integrations"


@pytest.mark.slow  # 真实拉起插件子进程，默认跳过（pytest -m slow 显式运行）
def test_layer_starts_and_lists_plugins():
    layer = IntegrationLayer(
        plugin_dir=INTEGRATIONS_TESTS_DIR,
        api_version="1",
        rpc_timeout=15.0,
        max_restarts=1,
    )

    async def go():
        try:
            await layer.start()
            plugins = layer.list_plugins()
            ids = [p["id"] for p in plugins]
            assert "echo" in ids
            echo = next(p for p in plugins if p["id"] == "echo")
            assert echo["alive"] is True
            assert "output_sink" in echo["capabilities"]
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())


@pytest.mark.slow  # 真实拉起插件子进程，默认跳过
def test_layer_broadcasts_via_sink_manager():
    layer = IntegrationLayer(
        plugin_dir=INTEGRATIONS_TESTS_DIR,
        api_version="1", rpc_timeout=15.0, max_restarts=1,
    )

    async def go():
        try:
            await layer.start()
            await layer.sink_manager.broadcast("测试消息", "m1")
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())


def test_layer_list_plugins_when_not_started_shows_dead():
    layer = IntegrationLayer(plugin_dir=INTEGRATIONS_TESTS_DIR)

    plugins = layer.list_plugins()
    echo = next(p for p in plugins if p["id"] == "echo")
    assert echo["alive"] is False  # 未 start
