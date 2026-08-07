"""METHOD_ROUTE 常量测试。"""

from app.integration.rpc_protocol import METHOD_ROUTE, METHOD_SPEAK, build_request


def test_method_route_constant():
    assert METHOD_ROUTE == "router.handle"


def test_build_request_with_route():
    req = build_request(msg_id=1, method=METHOD_ROUTE, params={"text": "hi"})
    assert req["method"] == "router.handle"
    assert req["params"] == {"text": "hi"}
