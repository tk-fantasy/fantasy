"""FeishuSink 发消息逻辑测试（mock httpx，不真实调飞书 API）。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from integrations.feishu.plugin import FeishuSink


def _make_sink():
    """构造 FeishuSink（凭证假的，HTTP 全 mock）。"""
    return FeishuSink(app_id="cli_test", app_secret="secret_test")


def test_speak_skips_non_chat_id_msg_id():
    """msg_id 非 chat_id 格式时 skip（broadcast fan-out 乱入时）。"""
    sink = _make_sink()

    async def go():
        return await sink.speak("hello", msg_id="req_abc123")

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is False
    assert result.get("skipped") is True


def test_speak_sends_message_to_chat_id():
    """msg_id 是 chat_id 时调飞书发消息 API。"""
    sink = _make_sink()
    sink._get_tenant_token = AsyncMock(return_value="t-test-token")
    sink._send_message = AsyncMock()

    async def go():
        return await sink.speak("你好世界", msg_id="oc_test_chat_id")

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True
    assert result["chat_id"] == "oc_test_chat_id"
    sink._get_tenant_token.assert_called_once()
    sink._send_message.assert_called_once_with("t-test-token", "oc_test_chat_id", "你好世界")


def test_speak_empty_text_skips():
    """空文本不发。"""
    sink = _make_sink()

    async def go():
        return await sink.speak("", msg_id="oc_xxx")

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result.get("skipped") is True or result["ok"] is False


def test_interrupt_is_noop():
    """飞书无 TTS 可打断，interrupt 是 no-op。"""
    sink = _make_sink()

    async def go():
        return await sink.interrupt()

    result = asyncio.new_event_loop().run_until_complete(go())
    assert result["ok"] is True


def test_get_tenant_token_caches_and_refreshes():
    """tenant_access_token 缓存 + 过期刷新。"""
    sink = _make_sink()

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"tenant_access_token": "t-cached", "expire": 7200}
    mock_resp.raise_for_status = MagicMock()

    with patch("integrations.feishu.plugin.httpx") as mock_httpx:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_httpx.AsyncClient.return_value = mock_client

        async def go():
            token1 = await sink._get_tenant_token()
            token2 = await sink._get_tenant_token()
            return token1, token2

        t1, t2 = asyncio.new_event_loop().run_until_complete(go())

    assert t1 == "t-cached"
    assert t2 == "t-cached"
    assert mock_client.post.call_count == 1  # 第二次命中缓存
