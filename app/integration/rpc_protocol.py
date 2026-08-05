"""JSON-RPC 2.0 over stdio 协议常量与消息构造。

方向 1（Aether → 插件）使用的方法名定义于此。
Phase 1 只实现方向 1，Phase 3 补方向 2（插件 → Aether 反向调用）。
"""

import json

# ── 方法名常量（方向 1: Aether → 插件）──
METHOD_HANDSHAKE = "handshake"
METHOD_SPEAK = "sink.speak"
METHOD_INTERRUPT = "sink.interrupt"
METHOD_HEALTH = "health.check"
METHOD_SHUTDOWN = "shutdown"

JSONRPC_VERSION = "2.0"


def build_request(msg_id: int, method: str, params: dict | None = None) -> dict:
    """构造 JSON-RPC 2.0 请求。"""
    msg: dict = {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def build_response(msg_id: int, result: dict) -> dict:
    """构造 JSON-RPC 2.0 成功响应。"""
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id, "result": result}


def build_error(msg_id: int, code: int, message: str) -> dict:
    """构造 JSON-RPC 2.0 错误响应。"""
    return {"jsonrpc": JSONRPC_VERSION, "id": msg_id,
            "error": {"code": code, "message": message}}


def parse_message(raw: str) -> dict | None:
    """解析一行 stdio 输出为消息 dict。

    非 JSON / 空行返回 None（调用方应跳过或当日志处理）。
    """
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
