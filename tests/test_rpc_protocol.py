"""JSON-RPC 2.0 协议纯函数测试。"""

import json

from app.integration.rpc_protocol import (
    build_request, build_response, build_error,
    parse_message, METHOD_SPEAK, METHOD_INTERRUPT,
    METHOD_HANDSHAKE, METHOD_SHUTDOWN,
)


def test_build_request_with_params():
    msg = build_request(msg_id=1, method="sink.speak", params={"text": "hi"})
    assert msg == {"jsonrpc": "2.0", "id": 1, "method": "sink.speak", "params": {"text": "hi"}}


def test_build_request_without_params():
    msg = build_request(msg_id=2, method="handshake")
    assert msg == {"jsonrpc": "2.0", "id": 2, "method": "handshake"}


def test_build_response():
    msg = build_response(msg_id=1, result={"ok": True})
    assert msg == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_build_error():
    msg = build_error(msg_id=1, code=-32601, message="method not found")
    assert msg == {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}}


def test_parse_message_valid():
    line = json.dumps({"jsonrpc": "2.0", "id": 5, "method": METHOD_SPEAK, "params": {}})
    msg = parse_message(line)
    assert msg is not None
    assert msg["id"] == 5
    assert msg["method"] == METHOD_SPEAK


def test_parse_message_invalid_json_returns_none():
    assert parse_message("not json{") is None


def test_parse_message_empty_line_returns_none():
    assert parse_message("") is None
    assert parse_message("   \n") is None


def test_method_constants():
    assert METHOD_SPEAK == "sink.speak"
    assert METHOD_INTERRUPT == "sink.interrupt"
    assert METHOD_HANDSHAKE == "handshake"
    assert METHOD_SHUTDOWN == "shutdown"
