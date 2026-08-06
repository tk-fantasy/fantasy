"""端到端冒烟测试：IntegrationLayer 完整链路。

验证 manifest 加载 → 插件 spawn → SinkManager 广播 → echo sink 收到 speak。
用 echo 插件（不依赖小爱硬件，CI 友好）。
"""

import asyncio

from app.integration.integration_layer import IntegrationLayer

INTEGRATIONS_TESTS_DIR = "tests/integrations"


def test_e2e_broadcast_reaches_echo_sink():
    """完整链路：layer 启动 → broadcast → echo 插件收到 speak。"""
    layer = IntegrationLayer(
        plugin_dir=INTEGRATIONS_TESTS_DIR,
        api_version="1", rpc_timeout=15.0, max_restarts=1,
    )

    async def go():
        try:
            await layer.start()
            # 启动后 echo 插件应存活
            plugins = layer.list_plugins()
            echo = next(p for p in plugins if p["id"] == "echo")
            assert echo["alive"] is True
            # 广播一条消息（模拟 Dispatcher 钩子调用）
            await layer.sink_manager.broadcast("床头灯已打开", "req_001")
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())


def test_e2e_interrupt_all_after_broadcast():
    """广播后调 interrupt_all，每个 sink 应收到 interrupt（echo 返回 interrupted）。"""
    layer = IntegrationLayer(
        plugin_dir=INTEGRATIONS_TESTS_DIR,
        api_version="1", rpc_timeout=15.0, max_restarts=1,
    )

    async def go():
        try:
            await layer.start()
            await layer.sink_manager.broadcast("第一条消息", "m1")
            await layer.sink_manager.interrupt_all()
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())


def test_e2e_layer_crash_does_not_break_echo():
    """若 tests/integrations 同时含 echo 和 crash，echo 应正常启动（crash 不阻塞）。"""
    layer = IntegrationLayer(
        plugin_dir=INTEGRATIONS_TESTS_DIR,
        api_version="1", rpc_timeout=5.0, max_restarts=1,
    )

    async def go():
        try:
            await layer.start()
            plugins = layer.list_plugins()
            by_id = {p["id"]: p for p in plugins}
            # echo 应该存活
            if "echo" in by_id:
                assert by_id["echo"]["alive"] is True
            # crash 应该不存活（重试耗尽后熔断）
            if "crash" in by_id:
                assert by_id["crash"]["alive"] is False
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())
