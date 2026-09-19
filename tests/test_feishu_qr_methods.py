"""飞书插件 call_method（qr_start/qr_poll/qr_cancel）测试。"""

import asyncio

import pytest

from integrations.feishu import main as feishu_main
from integrations.feishu import qr_setup


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset():
    qr_setup._session = None
    qr_setup._pending_credentials = None
    yield
    qr_setup._session = None
    qr_setup._pending_credentials = None


def test_call_method_unknown_rejected():
    assert _run(feishu_main.call_method("not_a_method", {}))["success"] is False


def test_call_method_framework_blocked():
    assert _run(feishu_main.call_method("sink.speak", {}))["success"] is False


def test_qr_start_proxies_session_fields(monkeypatch):
    async def fake_start():
        return {"qr_url": "https://x", "user_code": "AB12-CD34",
                "expires_in": 3600, "interval": 5}

    monkeypatch.setattr(qr_setup, "start_session", fake_start)
    result = _run(feishu_main.call_method("qr_start", {}))
    assert result["qr_url"] == "https://x"


def test_qr_poll_success_writes_config_and_keeps_other_fields(monkeypatch):
    # poll_once 的状态机在 test_feishu_qr_setup.py 已覆盖；这里只测
    # success 分支的处理：消费凭证 → 落库（保留其余字段）→ 声明热重启。
    async def fake_poll():
        return {"status": "success"}

    monkeypatch.setattr(qr_setup, "poll_once", fake_poll)
    qr_setup._pending_credentials = ("cli_aaa123", "sec_xyz")
    saved = {}
    monkeypatch.setattr("app.integration.config_helper.get_host_config",
                        lambda pid: {"notify_chat_id": "oc_kept"})

    def fake_set(pid, values):
        saved[pid] = dict(values)

    monkeypatch.setattr("app.integration.config_helper.set_host_config", fake_set)
    result = _run(feishu_main.call_method("qr_poll", {}))
    assert result["status"] == "success"
    assert result["config_changed"] is True
    assert result["app_id_masked"].startswith("cli_aa")
    assert saved["feishu"]["app_id"] == "cli_aaa123"
    assert saved["feishu"]["app_secret"] == "sec_xyz"
    assert saved["feishu"]["notify_chat_id"] == "oc_kept"  # 其余字段保留
    assert qr_setup.consume_result() is None               # 凭证已消费


def test_qr_poll_pending_passthrough(monkeypatch):
    async def fake_poll():
        return {"status": "pending"}

    monkeypatch.setattr(qr_setup, "poll_once", fake_poll)
    assert _run(feishu_main.call_method("qr_poll", {})) == {"status": "pending"}


def test_qr_start_error_becomes_failure_message(monkeypatch):
    async def bad_start():
        raise qr_setup.FeishuQrSetupError("当前飞书环境不支持扫码接入，请改用手动配置")

    monkeypatch.setattr(qr_setup, "start_session", bad_start)
    result = _run(feishu_main.call_method("qr_start", {}))
    assert result["success"] is False
    assert "手动配置" in result["message"]


def test_qr_cancel_clears_session():
    qr_setup._session = {"device_code": "d"}
    result = _run(feishu_main.call_method("qr_cancel", {}))
    assert result["status"] == "cancelled"
    assert qr_setup._session is None


def test_meta_declares_config_modal_contribution():
    from integrations.feishu import meta
    assert meta.UI_CONTRIBUTIONS == [
        {"slot": "plugin_config_modal", "type": "custom_component"}]
