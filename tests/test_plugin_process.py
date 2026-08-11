"""PluginProcess 单进程 stdio JSON-RPC 连接测试（spawn 真实 echo 子进程）。"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.integration.manifest_loader import load_manifests
from app.integration.plugin_process import PluginProcess, _sandbox_env
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


def test_plugin_process_handshake_failure_cleans_up_subprocess():
    """握手失败必须清理已 spawn 的子进程 + reader/stderr task。

    复现审查 #4：start() 在 _handshake() 抛异常后若无清理，supervisor 重试
    会累积存活子进程（每次失败泄漏一个）。验证：异常向上抛 + 进程已终止 +
    is_alive 为 False + 内部 reader/stderr task 已 done。
    """
    manifest = _load_echo_manifest()
    proc = PluginProcess(
        manifest=manifest,
        plugin_root=f"{INTEGRATIONS_TESTS_DIR}/echo",
        rpc_timeout=15.0,
    )

    async def go():
        with patch.object(proc, "_handshake", new=AsyncMock(side_effect=RuntimeError("handshake boom"))):
            with pytest.raises(RuntimeError, match="handshake boom"):
                await proc.start()
        # 清理后状态
        assert proc.is_alive is False
        # 子进程已被回收（stop 内 terminate→kill→wait）
        assert proc._process is not None
        assert proc._process.returncode is not None  # 已退出
        # reader/stderr task 已结束（被 cancel）
        for t in (proc._reader_task, proc._stderr_task):
            if t is not None:
                assert t.done()

    asyncio.new_event_loop().run_until_complete(go())
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


class TestEnvSandbox:
    """子进程环境沙箱（审查 #12-B）：白名单继承系统变量，排除宿主密钥。

    原代码 dict(os.environ) 全量继承 → 插件能读走 JWT_SECRET/RTSP_PASSWORD
    等宿主密钥。改白名单后，只保留运行必需的系统变量，凭证走 manifest.secrets 注入。
    """

    def test_sandbox_excludes_host_secrets(self, monkeypatch):
        """宿主密钥不应进入子进程环境。"""
        monkeypatch.setenv("JWT_SECRET", "super-secret-jwt")
        monkeypatch.setenv("RTSP_PASSWORD", "camera-pwd")
        monkeypatch.setenv("PTZ_PASSWORD", "ptz-pwd")
        env = _sandbox_env()
        assert "JWT_SECRET" not in env
        assert "RTSP_PASSWORD" not in env
        assert "PTZ_PASSWORD" not in env

    def test_sandbox_keeps_system_vars(self, monkeypatch):
        """系统必需变量应保留（PATH/SYSTEMROOT/TEMP 等）。"""
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = _sandbox_env()
        assert env["PATH"] == "/usr/bin:/bin"

    def test_plugin_process_uses_sandbox(self, monkeypatch):
        """PluginProcess 构造的 _env 不含宿主密钥。"""
        monkeypatch.setenv("JWT_SECRET", "should-not-leak")
        manifest = _load_echo_manifest()
        proc = PluginProcess(
            manifest=manifest,
            plugin_root=f"{INTEGRATIONS_TESTS_DIR}/echo",
        )
        assert "JWT_SECRET" not in proc._env

    def test_plugin_process_keeps_injected_credentials(self, monkeypatch):
        """env 参数注入的凭证（manifest.secrets）仍能进入子进程。"""
        monkeypatch.setenv("JWT_SECRET", "host-secret")  # 应被排除
        manifest = _load_echo_manifest()
        proc = PluginProcess(
            manifest=manifest,
            plugin_root=f"{INTEGRATIONS_TESTS_DIR}/echo",
            env={"AETHER_HA_TOKEN": "plugin-allowed-token"},  # 应保留
        )
        assert proc._env.get("AETHER_HA_TOKEN") == "plugin-allowed-token"
        assert "JWT_SECRET" not in proc._env

