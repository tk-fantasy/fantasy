from __future__ import annotations

import asyncio
import json
import logging
import shutil

from ..core.exceptions import AppException

logger = logging.getLogger(__name__)


class ExternalMCPServerError(AppException):
    def __init__(self, message: str = "外部 MCP server 异常") -> None:
        super().__init__(message, code="external_mcp_error", http_status=502)


class ExternalMCPServer:
    """外部 MCP server 客户端。

    通过 subprocess stdio 走 JSON-RPC 2.0 与 MCP server 通信，
    支持 initialize 握手、tools/list、tools/call。

    用法:
        server = ExternalMCPServer("time-mcp", "npx", ["-y", "time-mcp"])
        await server.start()
        for tool in await server.list_tools():
            print(tool["name"])
        content = await server.call_tool("current_time", {"tz_offset_hours": 8})  # 示例，实际由配置决定
    """

    def __init__(self, name: str, cmd: str, args: list[str] | None = None):
        self.name = name
        resolved = shutil.which(cmd)
        if not resolved:
            raise ExternalMCPServerError(f"Command not found in PATH: {cmd}")
        self._cmd = resolved
        self._args = list(args or [])
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._initialized = False
        self._tools: list[dict] = []

    async def start(self) -> None:
        logger.info("Starting external MCP server", extra={"name": self.name, "cmd": self._cmd, "args": self._args})
        self._process = await asyncio.create_subprocess_exec(
            self._cmd,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    async def _read_stdout(self) -> None:
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                logger.debug("External MCP %s << %s", self.name, raw[:200])
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from external MCP %s: %s", self.name, raw[:120])
                    continue
                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    self._pending[rid].set_result(msg)
        finally:
            err = ExternalMCPServerError(f"External MCP server disconnected: {self.name}")
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                logger.debug("External MCP %s stderr: %s", self.name, line.decode("utf-8", errors="replace").rstrip()[:200])
        except Exception:
            pass

    async def _send_request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        if self._process is None or self._process.stdin.is_closing():
            raise ExternalMCPServerError("External MCP server not running")
        self._request_id += 1
        rid = self._request_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params:
            payload["params"] = params
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[rid] = future
        try:
            line = json.dumps(payload, ensure_ascii=False)
            logger.debug("External MCP %s >> %s", self.name, line[:200])
            self._process.stdin.write((line + "\n").encode("utf-8"))
            await asyncio.wait_for(self._process.stdin.drain(), timeout=timeout)
            result = await asyncio.wait_for(future, timeout=timeout)
            if "error" in result:
                err = result["error"]
                raise ExternalMCPServerError(f"MCP error: {err.get('message', err)}")
            return result.get("result", {})
        finally:
            self._pending.pop(rid, None)

    async def _initialize(self) -> None:
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "aether", "version": "1.0"},
        })
        self._initialized = True
        self._process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await self._process.stdin.drain()

    async def list_tools(self) -> list[dict]:
        """获取外部 server 的所有工具列表。"""
        if not self._initialized:
            await self._initialize()
        result = await self._send_request("tools/list")
        self._tools = result.get("tools", [])
        return self._tools

    async def call_tool(self, name: str, arguments: dict | None = None) -> list[dict]:
        """调用一个外部工具，返回 content 列表。"""
        result = await self._send_request("tools/call", {"name": name, "arguments": arguments or {}})
        return result.get("content", [])

    async def stop(self) -> None:
        logger.info("Stopping external MCP server", extra={"name": self.name})
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._process:
            try:
                # 先尝试优雅关闭（SIGTERM）
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    # 超时未退出，强制 kill（SIGKILL）
                    logger.warning("MCP server did not exit gracefully, forcing kill", extra={"name": self.name})
                    self._process.kill()
                    await self._process.wait()
            except Exception:
                pass
            # 关闭 stdin/stdout/stderr 管道
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                if stream and not stream.at_eof():
                    try:
                        stream.close()
                    except Exception:
                        pass


