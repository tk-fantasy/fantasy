"""PluginSupervisor 生命周期测试：启动成功 + 崩溃熔断。"""

import asyncio

import pytest

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_supervisor import PluginSupervisor

# 真实拉起插件子进程（含崩溃重试退避），默认跳过（pytest -m slow 显式运行）
pytestmark = pytest.mark.slow

INTEGRATIONS_TESTS_DIR = "tests/integrations"


def _load(manifest_id: str):
    manifests = load_manifests(INTEGRATIONS_TESTS_DIR, api_version="1")
    return next(m for m in manifests if m.id == manifest_id)


def test_supervisor_starts_echo_plugin():
    echo = _load("echo")
    sup = PluginSupervisor(rpc_timeout=15.0, max_restarts=3)

    async def go():
        try:
            await sup.start_all([echo], INTEGRATIONS_TESTS_DIR)
            proc = sup.get_process("echo")
            assert proc is not None
            assert proc.is_alive is True
        finally:
            await sup.stop_all()

    asyncio.new_event_loop().run_until_complete(go())


def test_supervisor_crash_plugin_disables_after_retries():
    """crash 插件握手超时，重试耗尽后不在 running 列表。"""
    crash = _load("crash")
    # max_restarts=1，rpc_timeout 短一点加速测试
    sup = PluginSupervisor(rpc_timeout=3.0, max_restarts=1)

    async def go():
        await sup.start_all([crash], INTEGRATIONS_TESTS_DIR)
        running = sup.get_running_manifests()
        assert all(m.id != "crash" for m in running), "崩溃插件不应在 running 列表"
        await sup.stop_all()

    asyncio.new_event_loop().run_until_complete(go())


def test_supervisor_mixed_start_echo_succeeds_crash_fails():
    """混合启动：echo 成功，crash 失败，但 crash 不阻塞 echo。"""
    echo = _load("echo")
    crash = _load("crash")
    sup = PluginSupervisor(rpc_timeout=3.0, max_restarts=1)

    async def go():
        try:
            await sup.start_all([crash, echo], INTEGRATIONS_TESTS_DIR)
            # echo 应该成功
            assert sup.get_process("echo") is not None
            assert sup.get_process("echo").is_alive is True
            # crash 应该失败
            assert sup.get_process("crash") is None
        finally:
            await sup.stop_all()

    asyncio.new_event_loop().run_until_complete(go())
