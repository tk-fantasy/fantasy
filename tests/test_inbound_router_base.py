"""InboundRouter ABC + plugin_base router.handle 路由测试。"""

import asyncio

from app.integration.sdk.plugin_base import IntegrationPlugin
from app.integration.sdk.router_base import InboundRouter
from app.integration.rpc_protocol import METHOD_ROUTE, METHOD_SPEAK


class FakeRouter(InboundRouter):
    """测试用 router，记录收到的 text。"""
    def __init__(self):
        self.received: list[str] = []

    async def route(self, text: str) -> dict:
        self.received.append(text)
        return {"ok": True, "executed": text}


class FakePlugin(IntegrationPlugin):
    def setup(self, manifest_dict):
        self.manifest = manifest_dict
        self.routers = [FakeRouter()]


def test_inbound_router_is_abstract():
    """InboundRouter 不能直接实例化（抽象基类）。"""
    import pytest
    with pytest.raises(TypeError):
        InboundRouter()


def test_plugin_handles_router_handle():
    """plugin.handle(METHOD_ROUTE) 调用 router.route。"""
    plugin = FakePlugin()
    plugin.setup({})

    async def go():
        result = await plugin.handle(METHOD_ROUTE, {"text": "播放音乐"})
        assert result == {"ok": True, "executed": "播放音乐"}
        assert plugin.routers[0].received == ["播放音乐"]

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_router_handle_no_router_registered():
    """没注册 router 时返回 error。"""
    plugin = IntegrationPlugin()  # 没设 routers

    async def go():
        result = await plugin.handle(METHOD_ROUTE, {"text": "hi"})
        assert "error" in result

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_unknown_method_still_errors():
    """未知方法仍返回 error。"""
    plugin = IntegrationPlugin()

    async def go():
        result = await plugin.handle("bogus.method", {})
        assert "error" in result

    asyncio.new_event_loop().run_until_complete(go())
