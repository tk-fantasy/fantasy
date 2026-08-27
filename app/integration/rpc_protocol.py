"""JSON-RPC 2.0 over stdio 协议常量与消息构造。

两个方向的请求方法名都定义于此：
- 方向 1（Aether → 插件）：handshake / sink.speak / sink.interrupt / router.handle / ...
- 方向 2（插件 → Aether，Phase 3）：ha.call_service / ha.get_states / llm.chat / sink.broadcast ...

id 奇偶约定（双方零冲突）：
- 方向 1 请求（Aether 发起）用 **偶数** id，从 2 开始（``plugin_process._next_id`` 从 1 自增到 2）。
- 方向 2 请求（插件发起）用 **奇数** id，从 1 开始（``stdio_runtime`` 反向 _next_id 从 0 自增到 1）。
- 双方各自维护 pending future map，奇偶天然不撞；响应原路返回，按 id 配对。
"""

import json

# ── 方法名常量（方向 1: Aether → 插件）──
METHOD_HANDSHAKE = "handshake"
METHOD_SPEAK = "sink.speak"
METHOD_INTERRUPT = "sink.interrupt"
METHOD_ROUTE = "router.handle"
METHOD_SHUTDOWN = "shutdown"  # 停止通知：plugin_process.stop 发送；插件侧无内置
# handler（可经 register_method("shutdown", ...) 自定义），实际停止靠关 stdin。

# ── 方法名常量（方向 2: 插件 → Aether 反向调用）──
METHOD_HOST_HA_CALL = "ha.call_service"
METHOD_HOST_HA_STATES = "ha.get_states"
METHOD_HOST_HA_DEVICES = "ha.get_devices_grouped"
METHOD_HOST_LLM_CHAT = "llm.chat"
METHOD_HOST_BROADCAST = "sink.broadcast"
METHOD_HOST_CAM_REGISTER = "camera.register"
METHOD_HOST_CAM_PUSH = "camera.push_frame"
METHOD_HOST_CAM_UNREGISTER = "camera.unregister"
METHOD_HOST_CAM_SET_FLAGS = "camera.set_flags"

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
