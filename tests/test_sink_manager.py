"""SinkManager 广播 fan-out 测试。"""

import asyncio

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_supervisor import PluginSupervisor
from app.integration.sink_manager import SinkManager

INTEGRATIONS_TESTS_DIR = "tests/integrations"


def _load_echo():
    manifests = load_manifests(INTEGRATIONS_TESTS_DIR, api_version="1")
    return next(m for m in manifests if m.id == "echo")


def test_broadcast_reaches_echo_sink():
    echo = _load_echo()
    sup = PluginSupervisor(rpc_timeout=15.0, max_restarts=1)
    manager = SinkManager(sup)

    async def go():
        try:
            await sup.start_all([echo], INTEGRATIONS_TESTS_DIR)
            await manager.broadcast("床头灯已打开", "msg1")
        finally:
            await sup.stop_all()

    asyncio.new_event_loop().run_until_complete(go())


def test_interrupt_all_calls_each_sink():
    echo = _load_echo()
    sup = PluginSupervisor(rpc_timeout=15.0, max_restarts=1)
    manager = SinkManager(sup)

    async def go():
        try:
            await sup.start_all([echo], INTEGRATIONS_TESTS_DIR)
            await manager.interrupt_all()
        finally:
            await sup.stop_all()

    asyncio.new_event_loop().run_until_complete(go())


def test_broadcast_with_no_sinks_does_not_raise():
    sup = PluginSupervisor(rpc_timeout=5.0)
    manager = SinkManager(sup)

    async def go():
        await manager.broadcast("hello")
        await manager.interrupt_all()

    asyncio.new_event_loop().run_until_complete(go())
