"""PluginBase 对 METHOD_SHUTDOWN 的内置应答 + 自定义覆盖优先级测试。

宿主 plugin_process.stop 先发 shutdown 通知再关 stdin。内置分支调用
on_shutdown 钩子（默认 no-op，子类覆写做清理）并应答 {"ok": True}——
此前通知落到 unknown-method 错误，钩子从未被接线。
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.integration.sdk.plugin_base import IntegrationPlugin


def _make_plugin() -> IntegrationPlugin:
    plugin = IntegrationPlugin.__new__(IntegrationPlugin)
    plugin.manifest = {"id": "t", "version": "1", "capabilities": []}
    plugin._custom_methods = {}
    plugin.sinks = []
    plugin.routers = []
    return plugin


@pytest.mark.asyncio
async def test_shutdown_returns_ok_and_calls_hook():
    plugin = _make_plugin()
    plugin.on_shutdown = AsyncMock(return_value=None)
    assert await plugin.handle("shutdown", {}) == {"ok": True}
    plugin.on_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_custom_register_method_overrides_builtin():
    plugin = _make_plugin()
    custom = AsyncMock(return_value={"ok": True, "custom": True})
    plugin.register_method("shutdown", custom)
    plugin.on_shutdown = AsyncMock()

    result = await plugin.handle("shutdown", {})

    assert result == {"ok": True, "custom": True}
    custom.assert_awaited_once()
    plugin.on_shutdown.assert_not_awaited()  # custom 分支短路，内置钩子不再调


@pytest.mark.asyncio
async def test_on_shutdown_exception_does_not_block_stop():
    """清理函数抛异常：吞掉并照常应答 ok（stderr 已留栈），不阻塞停止流程。"""
    plugin = _make_plugin()
    plugin.on_shutdown = AsyncMock(side_effect=RuntimeError("flush failed"))
    assert await plugin.handle("shutdown", {}) == {"ok": True}


@pytest.mark.asyncio
async def test_unknown_method_still_errors():
    plugin = _make_plugin()
    result = await plugin.handle("no.such.method", {})
    assert "error" in result
