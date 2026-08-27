"""app/mcp/external_mcp_server.py 测试：外部 MCP stdio JSON-RPC 客户端。

用 Python 写一个伪 MCP server（stdin/stdout 走 JSON-RPC 2.0），由
ExternalMCPServer 以真实子进程方式拉起——覆盖握手、通知容忍、工具列表、
调用、服务端错误、非 JSON 输出容忍、断连时挂起请求失败、优雅停止。
单测部分（cmd 不存在 / 未启动就调用）不拉子进程。
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from app.mcp.external_mcp_server import ExternalMCPServer, ExternalMCPServerError

# 伪 MCP server 源码：运行期写入 tmp 再以子进程启动，避免测试目录被收集。
_FAKE_SERVER_SOURCE = '''\
"""伪外部 MCP server：stdin/stdout JSON-RPC 2.0，仅供测试。"""
import json
import sys

# 启动即吐一行非 JSON 垃圾，验证客户端 reader 能容忍并继续工作
print("<not-json garbage>", flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except ValueError:
        print("still-garbage", flush=True)
        continue
    rid = msg.get("id")
    method = msg.get("method", "")
    if rid is None:
        continue  # notification（如 notifications/initialized）：不回复
    def _resp(result=None, error=None):
        body = {"jsonrpc": "2.0", "id": rid}
        body["result" if error is None else "error"] = error if error is not None else result
        return json.dumps(body, ensure_ascii=False)
    if method == "initialize":
        print(_resp(result={"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "serverInfo": {"name": "fake-mcp", "version": "0"}}), flush=True)
    elif method == "tools/list":
        print(_resp(result={"tools": [
            {"name": "echo", "description": "回声"},
            {"name": "boom", "description": "总是报错"},
        ]}), flush=True)
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name", "")
        if name == "boom":
            print(_resp(error={"code": -32603, "message": "炸了"}), flush=True)
        else:
            args = params.get("arguments") or {}
            text = "echo:" + json.dumps(args, ensure_ascii=False, sort_keys=True)
            print(_resp(result={"content": [{"type": "text", "text": text}]}), flush=True)
    else:
        print(_resp(error={"code": -32601, "message": f"unknown method {method}"}), flush=True)
'''


@pytest.fixture(scope="session")
def fake_mcp_script(tmp_path_factory):
    path = tmp_path_factory.mktemp("mcp") / "fake_mcp_server.py"
    path.write_text(_FAKE_SERVER_SOURCE, encoding="utf-8")
    return str(path)


@pytest.fixture()
async def server(fake_mcp_script):
    srv = ExternalMCPServer("fake-mcp", sys.executable, [fake_mcp_script])
    await asyncio.wait_for(srv.start(), timeout=15)
    try:
        yield srv
    finally:
        await asyncio.wait_for(srv.stop(), timeout=10)


# --------------- 单元：无需子进程 ---------------

def test_unknown_command_rejected():
    with pytest.raises(ExternalMCPServerError, match="Command not found"):
        ExternalMCPServer("x", "definitely-not-a-real-cmd-xyz")


async def test_send_request_before_start_raises():
    srv = ExternalMCPServer("x", sys.executable, ["-c", "pass"])
    with pytest.raises(ExternalMCPServerError, match="not running"):
        await srv.list_tools()


# --------------- 集成：真实伪 server 子进程 ---------------

async def test_start_initializes_and_marks_state(server):
    assert server._initialized is True
    assert server._request_id >= 1  # initialize 已发过一次请求


async def test_list_tools(server):
    tools = await asyncio.wait_for(server.list_tools(), timeout=10)
    names = {t["name"] for t in tools}
    assert names == {"echo", "boom"}


async def test_call_tool_round_trip_with_arguments(server):
    content = await asyncio.wait_for(
        server.call_tool("echo", {"who": "aether", "n": 1}), timeout=10
    )
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"].removeprefix("echo:"))
    assert payload == {"who": "aether", "n": 1}


async def test_call_tool_without_arguments_defaults_empty(server):
    content = await asyncio.wait_for(server.call_tool("echo"), timeout=10)
    assert json.loads(content[0]["text"].removeprefix("echo:")) == {}


async def test_server_side_error_raises_mcp_error(server):
    with pytest.raises(ExternalMCPServerError, match="炸了"):
        await asyncio.wait_for(server.call_tool("boom"), timeout=10)


async def test_garbage_stdout_does_not_break_stream(server):
    """启动时的垃圾行被忽略后，协议仍正常收发。"""
    tools = await asyncio.wait_for(server.list_tools(), timeout=10)
    assert tools


async def test_stop_terminates_process(fake_mcp_script):
    srv = ExternalMCPServer("fake-mcp", sys.executable, [fake_mcp_script])
    await asyncio.wait_for(srv.start(), timeout=15)
    proc = srv._process
    await asyncio.wait_for(srv.stop(), timeout=10)
    assert proc.returncode is not None  # 进程确已退出


async def test_disconnect_fails_pending_request(server):
    """进程退出导致 stdout EOF 时，在途请求立即收到 ExternalMCPServerError。"""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    server._pending[999] = fut
    server._process.kill()
    with pytest.raises(ExternalMCPServerError, match="disconnected"):
        await asyncio.wait_for(fut, timeout=10)
