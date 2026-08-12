"""插件进程入口：并发 stdio JSON-RPC runtime（Phase 3）。

并发模型（解死锁）：
- 后台 reader task：常驻 ``run_in_executor`` 阻塞读 stdin。Windows Proactor 对匿名管道
  （子进程 stdio）的 async 读不生效——连接成功但数据永不到达，插件会静默卡死到握手
  超时；必须走线程阻塞读，跨平台都可靠（插件流量是低频 JSON-RPC 行）。读到消息后分流：
    · 带 ``method`` = 方向 1 请求（宿主→插件）→ 起独立 task 并发执行 ``plugin.handle``
    · 带 ``id`` 且在反向 pending = 方向 2 响应（宿主回插件的反向调用结果）→ resolve future
- writer task：从 ``asyncio.Queue`` 串行写 stdout（避免并发写交错）。正向响应 + 反向请求都入队。
- ``host_call``：插件发起反向调用（方向 2），分配奇数 id，注册 future，入队请求，await 响应。

旧实现是 ``read → await plugin.handle → write`` 严格串行：handle 内反向调宿主并 await 响应时
没人读 stdin → 必死锁。reader/handle/writer 三者解耦后，handle 内反向调用不再阻塞 stdin 读取。
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Type

from ..rpc_protocol import (
    METHOD_HANDSHAKE,
    build_request,
    build_response,
    parse_message,
)
from .plugin_base import HostProxy, IntegrationPlugin


class _StdioRuntime:
    """单插件进程的并发 stdio runtime。

    持有反向调用 pending map + 输出队列；reader/writer/host_call 三者通过队列与 future 解耦。
    """

    def __init__(self, plugin: IntegrationPlugin, reverse_timeout: float = 30.0) -> None:
        self._plugin = plugin
        self._loop = asyncio.get_running_loop()
        self._reverse_timeout = reverse_timeout
        self._manifest: dict = plugin.manifest or {}
        # 方向 2 反向请求的 pending future（奇数 id → future）
        self._pending_reverse: dict[int, asyncio.Future] = {}
        self._next_reverse_id = 1  # 1, 3, 5...（奇数，与宿主偶数请求 id 不撞）
        self._queue: asyncio.Queue = asyncio.Queue()

    async def run(self) -> None:
        """启动 reader/writer，stdin 关闭即收尾。"""
        reader = self._loop.create_task(self._reader())
        writer = self._loop.create_task(self._writer())
        try:
            await reader  # stdin 关闭（readline 返回 b''）即退出
        finally:
            await self._queue.put(None)  # 通知 writer 停
            try:
                await writer
            except Exception:  # noqa: BLE001 — 收尾阶段任何错误都不该掩盖退出
                pass
            # 失败未完成的反向 future（进程已退，永远等不到响应）
            for fut in list(self._pending_reverse.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("插件进程退出，反向调用未完成"))

    async def _reader(self) -> None:
        """后台读 stdin（线程阻塞读）。分流方向 1 请求 vs 方向 2 反向响应。"""
        while True:
            line = await self._loop.run_in_executor(None, sys.stdin.buffer.readline)
            if not line:
                break  # stdin 关闭
            msg = parse_message(line.decode("utf-8", errors="replace"))
            if msg is None:
                continue
            if "method" in msg:
                # 方向 1：宿主→插件请求 → 并发处理（不 await，reader 立即继续读）
                self._loop.create_task(self._handle_request(msg))
            else:
                # 方向 2 响应：按奇数 id 配对到反向 pending future（含 error 字段）
                rid = msg.get("id")
                if rid is not None and rid in self._pending_reverse:
                    fut = self._pending_reverse.pop(rid)
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(f"宿主反向调用错误: {msg['error']}")
                            )
                        else:
                            fut.set_result(msg.get("result", {}))

    async def _writer(self) -> None:
        """串行写 stdout：正向响应 + 反向请求都经此队列，避免并发写交错。"""
        while True:
            payload = await self._queue.get()
            if payload is None:
                break  # run() 收尾发的停止哨兵
            # 必须走 buffer 写原始 utf-8 字节：文本模式 stdout 会用本地编码
            # （如 Windows cp936）编码中文，宿主按 utf-8 解析会乱码。
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def _handle_request(self, msg: dict) -> None:
        """处理方向 1 请求：handshake 特判，其余走 plugin.handle。"""
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}
        try:
            if method == METHOD_HANDSHAKE:
                result = {
                    "plugin_id": self._manifest.get("id"),
                    "plugin_version": self._manifest.get("version"),
                    "ready": True,
                }
            else:
                result = await self._plugin.handle(method, params)
        except Exception as exc:  # 插件代码异常不能崩 runtime
            # 回传给宿主的 error 只保留异常类型名——message 可能含敏感信息（路径/token 片段）。
            # 完整 traceback 写 stderr，宿主 _drain_stderr 以 debug 记录（不外泄）。
            print(f"[{self._manifest.get('id', '?')}] plugin error: "
                  f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                  file=sys.stderr, flush=True)
            result = {"error": type(exc).__name__}
        # 业务错误也放 result 字段返回（Phase 1 简化约定，保持向后兼容）
        await self._queue.put(build_response(msg_id, result))

    async def host_call(self, method: str, params: dict | None = None,
                        timeout: float | None = None) -> dict:
        """方向 2：插件发起反向调用宿主能力。分配奇数 id，入队请求，等响应。"""
        rid = self._next_reverse_id
        self._next_reverse_id += 2
        future: asyncio.Future = self._loop.create_future()
        self._pending_reverse[rid] = future
        await self._queue.put(build_request(rid, method, params))
        try:
            return await asyncio.wait_for(
                future, timeout=timeout if timeout is not None else self._reverse_timeout
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"反向调用 {method} 超时")
        finally:
            self._pending_reverse.pop(rid, None)


async def run_stdio_plugin(
    plugin_cls: Type[IntegrationPlugin],
    manifest_path: str,
) -> None:
    """插件进程入口：实例化 → 注入 HostProxy → setup → 并发 runtime 主循环。"""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    plugin = plugin_cls()
    plugin.manifest = manifest  # 保证 setup 前 manifest 可用
    runtime = _StdioRuntime(plugin)
    # 关键：host 必须在 setup 前注入——插件 setup 内即用 self.host.ha 构造 sink（如小爱）。
    plugin.host = HostProxy(runtime.host_call)
    plugin.setup(manifest)
    # 显式赋值 manifest，保证 capability 校验有数据（不依赖子类调 super().setup）。
    plugin.manifest = manifest
    await runtime.run()
