"""飞书扫码 qr_setup 状态机测试（mock 飞书端点，不触网）。"""

import asyncio
import time

import pytest

from integrations.feishu import qr_setup


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_session():
    qr_setup._session = None
    qr_setup._pending_credentials = None
    yield
    qr_setup._session = None
    qr_setup._pending_credentials = None


def _patch_feishu(monkeypatch, responses):
    """按调用顺序回放响应（Exception 表示网络异常）；返回请求体记录列表。"""
    calls = []
    iterator = iter(responses)

    async def fake_post(payload):
        calls.append(dict(payload))
        resp = next(iterator)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(qr_setup, "_post_registration", fake_post)
    return calls


def _mk_async_stub(responses):
    """不记录调用的按序回放桩（用于 poll 阶段重新打桩）。"""
    iterator = iter(responses)

    async def fake_post(payload):
        return next(iterator)

    return fake_post


def _allow_next_poll():
    """跳过限速窗口（模拟距上次轮询已过 interval）。"""
    if qr_setup._session is not None:
        qr_setup._session["last_poll_ts"] = 0.0


_INIT_OK = {"nonce": "n", "supported_auth_methods": ["private_key_jwt", "client_secret"]}
_BEGIN_OK = {
    "device_code": "dev123",
    "user_code": "AB12-CD34",
    "verification_uri": "https://open.feishu.cn/page/launcher",
    "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=AB12-CD34",
    "expires_in": 3600,
    "interval": 5,
}


def test_start_session_returns_qr_fields(monkeypatch):
    calls = _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    result = _run(qr_setup.start_session())
    assert result["qr_url"] == _BEGIN_OK["verification_uri_complete"]
    assert result["qr_svg_data_url"].startswith("data:image/svg+xml;base64,")
    assert result["user_code"] == "AB12-CD34"
    assert result["expires_in"] == 3600
    assert result["interval"] == 5
    # begin 参数按协议：PersonalAgent 原型 + client_secret
    assert calls[1]["action"] == "begin"
    assert calls[1]["archetype"] == "PersonalAgent"
    assert calls[1]["auth_method"] == "client_secret"
    assert qr_setup._session["device_code"] == "dev123"
    assert qr_setup._session["last_status"] == "pending"


def test_start_session_unsupported_auth_rejected(monkeypatch):
    _patch_feishu(monkeypatch, [{"supported_auth_methods": ["private_key_jwt"]}])
    with pytest.raises(qr_setup.FeishuQrSetupError):
        _run(qr_setup.start_session())


def test_start_session_bad_begin_rejected(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, {"no_device_code": True}])
    with pytest.raises(qr_setup.FeishuQrSetupError):
        _run(qr_setup.start_session())


def test_poll_without_session_returns_expired():
    result = _run(qr_setup.poll_once())
    assert result["status"] == "expired"


def test_poll_pending_then_scanned_then_success(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    monkeypatch.setattr(qr_setup, "_post_registration", _mk_async_stub([
        {"error": "authorization_pending"},
        {"user_info": {"open_id": "ou_x"}},
        {"client_id": "cli_aaa123", "client_secret": "sec_xyz"},
    ]))
    assert _run(qr_setup.poll_once())["status"] == "pending"
    _allow_next_poll()
    assert _run(qr_setup.poll_once())["status"] == "scanned"
    _allow_next_poll()
    success = _run(qr_setup.poll_once())
    assert success["status"] == "success"
    assert qr_setup.consume_result() == ("cli_aaa123", "sec_xyz")
    assert qr_setup.consume_result() is None          # 取走即失效
    assert qr_setup._session is None                  # 会话已关闭


def test_poll_rate_limit_returns_cached_without_request(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    qr_setup._session["last_poll_ts"] = time.monotonic()  # 刚轮询过
    calls = _patch_feishu(monkeypatch, [])                # 任何请求都不该发生
    assert _run(qr_setup.poll_once())["status"] == "pending"
    assert calls == []


def test_poll_slow_down_increases_interval(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    _patch_feishu(monkeypatch, [{"error": "slow_down"}])
    _run(qr_setup.poll_once())
    assert qr_setup._session["interval"] == 10


def test_poll_denied(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK, {"error": "access_denied"}])
    _run(qr_setup.start_session())
    result = _run(qr_setup.poll_once())
    assert result["status"] == "denied"
    assert qr_setup._session is None


def test_poll_expired_token(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK, {"error": "expired_token"}])
    _run(qr_setup.start_session())
    assert _run(qr_setup.poll_once())["status"] == "expired"


def test_poll_local_expiry(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    qr_setup._session["expires_at"] = time.monotonic() - 1
    assert _run(qr_setup.poll_once())["status"] == "expired"
    assert qr_setup._session is None


def test_poll_consecutive_errors_threshold(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    for _ in range(4):
        _patch_feishu(monkeypatch, [RuntimeError("net down")])
        _allow_next_poll()
        assert _run(qr_setup.poll_once())["status"] == "pending"  # 容错续等
    _patch_feishu(monkeypatch, [RuntimeError("net down")])
    _allow_next_poll()
    result = _run(qr_setup.poll_once())  # 第 5 次：报通道故障
    assert result["status"] == "error"
    assert qr_setup._session is None


def test_cancel_session_clears_state(monkeypatch):
    _patch_feishu(monkeypatch, [_INIT_OK, _BEGIN_OK])
    _run(qr_setup.start_session())
    qr_setup.cancel_session()
    assert qr_setup._session is None
    assert _run(qr_setup.poll_once())["status"] == "expired"
