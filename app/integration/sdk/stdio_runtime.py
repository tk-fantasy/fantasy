"""插件进程入口：从 stdin 读 JSON-RPC，分发到 plugin.handle，写 stdout。

复用宿主侧 rpc_protocol 的消息构造/解析。每个插件进程以此为主循环。
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Type

from ..rpc_protocol import METHOD_HANDSHAKE, build_response, parse_message
from .plugin_base import IntegrationPlugin


async def run_stdio_plugin(
    plugin_cls: Type[IntegrationPlugin],
    manifest_path: str,
) -> None:
    """插件进程主循环。

    读 stdin 一行一个 JSON-RPC 消息，handshake 之外的方法都走 plugin.handle。
    stdin 关闭即退出。
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    plugin = plugin_cls()
    plugin.setup(manifest)

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break  # stdin 关闭
        msg = parse_message(line.decode("utf-8", errors="replace"))
        if msg is None:
            continue
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        try:
            if method == METHOD_HANDSHAKE:
                result = {
                    "plugin_id": manifest.get("id"),
                    "plugin_version": manifest.get("version"),
                    "ready": True,
                }
            else:
                result = await plugin.handle(method, params)
        except Exception as exc:  # 插件代码异常不能崩 runtime
            result = {"error": f"{type(exc).__name__}: {exc}"}

        # Phase 1 简化：业务错误也放 result 字段返回，不使用 JSON-RPC error 字段
        response = build_response(msg_id, result)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
