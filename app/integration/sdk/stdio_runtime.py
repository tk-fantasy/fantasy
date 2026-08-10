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

    # Windows Proactor 下 connect_read_pipe 对匿名管道（子进程 stdio）读取
    # 不生效：连接成功但数据永远不会到达，插件会静默卡死到握手超时。
    # 改用线程阻塞读 stdin，跨平台都可靠（插件流量是低频 JSON-RPC 行）。
    loop = asyncio.get_running_loop()

    while True:
        line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
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
        # 必须走 buffer 写原始 utf-8 字节：文本模式 stdout 会用本地编码
        # （如 Windows cp936）编码中文，宿主按 utf-8 解析会乱码。
        sys.stdout.buffer.write(
            (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
        )
        sys.stdout.buffer.flush()
