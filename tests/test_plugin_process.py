"""PluginProcess 单进程 stdio JSON-RPC 连接测试（spawn 真实 echo 子进程）。"""

import asyncio

import pytest

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_process import PluginProcess
from app.integration.rpc_protocol import METHOD_SPEAK

INTEGRATIONS_TESTS_DIR = "tests/integrations"


def _load_echo_manifest():
    manifests = load_manifests(INTEGRATIONS_TESTS_DIR, api_version="1")
    return next(m for m in manifests if m.id == "echo")


def test_plugin_process_handshake_and_speak():
    manifest = _load_echo_manifest()
    proc = PluginProcess(
        manifest=manifest,
        plugin_root=f"{INTEGRATIONS_TESTS_DIR}/echo",
        rpc_timeout=15.0,
    )

    async def go():
        try:
            await proc.start()
            assert proc.is_alive is True

            result = await proc.call(METHOD_SPEAK, {"text": "hello", "msg_id": "m1"})
            assert result == {"spoken": "hello", "msg_id": "m1"}
        finally:
            await proc.stop()

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_process_call_after_stop_raises():
    manifest = _load_echo_manifest()
    proc = PluginProcess(
        manifest=manifest,
        plugin_root=f"{INTEGRATIONS_TESTS_DIR}/echo",
        rpc_timeout=15.0,
    )

    async def go():
        await proc.start()
        await proc.stop()
        assert proc.is_alive is False
        with pytest.raises(RuntimeError):
            await proc.call(METHOD_SPEAK, {"text": "x"})

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_process_multiple_calls():
    """连续多次调用都应正常配对响应。"""
    manifest = _load_echo_manifest()
    proc = PluginProcess(
        manifest=manifest,
        plugin_root=f"{INTEGRATIONS_TESTS_DIR}/echo",
        rpc_timeout=15.0,
    )

    async def go():
        try:
            await proc.start()
            r1 = await proc.call(METHOD_SPEAK, {"text": "第一条", "msg_id": "1"})
            r2 = await proc.call(METHOD_SPEAK, {"text": "第二条", "msg_id": "2"})
            assert r1 == {"spoken": "第一条", "msg_id": "1"}
            assert r2 == {"spoken": "第二条", "msg_id": "2"}
        finally:
            await proc.stop()

    asyncio.new_event_loop().run_until_complete(go())
