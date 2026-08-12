"""宿主侧方向 2 接收端单测：plugin_process._handle_reverse 与 _read_stdout 分流。

不 spawn 子进程；用 mock host_registry + 假 stdin/stdout 验证：
- _handle_reverse 把反向请求 dispatch 后写回响应（成功 / 权限拒绝 / 异常）。
- _read_stdout 收到带 method 的消息时异步分发反向请求，且仍能正常配对方向 1 响应。
"""

import asyncio
import json

import pytest

from app.integration.host_registry import HostMethodRegistry
from app.integration.plugin_process import PluginProcess
from app.integration.schema import Manifest


def _manifest(perms=None):
    return Manifest(
        id="p1", name="t", version="1", aether_api_version="1",
        permissions=perms or [],
    )


def _make_proc(perms=None, rpc_timeout=1.0):
    proc = PluginProcess(manifest=_manifest(perms), plugin_root=".", rpc_timeout=rpc_timeout)
    return proc


def _wire_write(proc):
    """替换 _write_line 为捕获写入。"""
    written = []

    async def fake_write(payload):
        written.append(payload)

    proc._write_line = fake_write
    return written


@pytest.mark.asyncio
async def test_handle_reverse_dispatches_and_writes_success():
    proc = _make_proc(perms=["ha"])
    reg = HostMethodRegistry()

    async def handler(params):
        return {"ok": True, "got": params}

    reg.register("ha.call_service", handler, required_permission="ha")
    proc._host_registry = reg
    written = _wire_write(proc)

    await proc._handle_reverse(
        {"id": 1, "method": "ha.call_service", "params": {"domain": "light"}}
    )

    assert len(written) == 1
    assert written[0]["id"] == 1
    assert written[0]["result"]["ok"] is True
    assert written[0]["result"]["got"] == {"domain": "light"}


@pytest.mark.asyncio
async def test_handle_reverse_permission_denied_writes_error():
    proc = _make_proc(perms=[])  # 未声明 ha 权限
    reg = HostMethodRegistry()

    async def handler(params):  # 不应被调用
        raise AssertionError("handler should not run")

    reg.register("ha.call_service", handler, required_permission="ha")
    proc._host_registry = reg
    written = _wire_write(proc)

    await proc._handle_reverse({"id": 3, "method": "ha.call_service", "params": {}})

    assert len(written) == 1
    assert "error" in written[0]
    assert written[0]["id"] == 3


@pytest.mark.asyncio
async def test_handle_reverse_handler_exception_writes_error():
    proc = _make_proc(perms=["ha"])
    reg = HostMethodRegistry()

    async def handler(params):
        raise ValueError("boom")

    reg.register("ha.call_service", handler, required_permission="ha")
    proc._host_registry = reg
    written = _wire_write(proc)

    await proc._handle_reverse({"id": 5, "method": "ha.call_service", "params": {}})

    assert "error" in written[0]
    assert written[0]["id"] == 5


@pytest.mark.asyncio
async def test_read_stdout_shunts_reverse_and_pairs_response():
    """reader：反向请求异步分发；方向 1 响应仍按 id 配对。"""
    proc = _make_proc(perms=["ha"])
    reg = HostMethodRegistry()

    async def handler(params):
        return {"ok": True}

    reg.register("ha.call_service", handler, required_permission="ha")
    proc._host_registry = reg
    written = _wire_write(proc)

    # 假 process.stdout：先一条反向请求，再一条方向 1 响应，再 EOF
    lines = [
        json.dumps({"id": 1, "method": "ha.call_service", "params": {}}).encode(),
        json.dumps({"id": 2, "result": {"hi": True}}).encode(),
        b"",
    ]

    class _FakeStdout:
        async def readline(self):
            return lines.pop(0) if lines else b""

    class _FakeProcess:
        stdout = _FakeStdout()

    proc._process = _FakeProcess()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    proc._pending[2] = fut  # 方向 1 响应 id=2 的 pending future

    await proc._read_stdout()  # EOF 后返回

    # 方向 1 响应已被配对
    assert fut.result() == {"hi": True}
    # 反向请求被 _handle_reverse 异步处理（create_task）——让事件循环跑一会儿
    await asyncio.sleep(0.1)
    assert len(written) == 1
    assert written[0]["result"]["ok"] is True


@pytest.mark.asyncio
async def test_read_stdout_response_error_field_sets_exception():
    """方向 1 响应带 error 字段 → future set_exception。"""
    proc = _make_proc()
    lines = [
        json.dumps({"id": 7, "error": {"code": -32000, "message": "boom"}}).encode(),
        b"",
    ]

    class _FakeStdout:
        async def readline(self):
            return lines.pop(0) if lines else b""

    class _FakeProcess:
        stdout = _FakeStdout()

    proc._process = _FakeProcess()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    proc._pending[7] = fut

    await proc._read_stdout()

    assert fut.done()
    with pytest.raises(RuntimeError):
        fut.result()
