"""Tests for stt_service — STT 配置已纳入 llm_keys 体系（type=stt）。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AppException
from app.services import stt_service


def _stt_key_entry(**overrides):
    """构造一条 type=stt 的 llm_keys 条目（模拟 resolve_key_for_role 解析后的返回）。"""
    base = {
        "id": "siliconflow-stt",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "FunAudioLLM/SenseVoiceSmall",
        "type": "stt",
        "chat_path": "",
        "embed_path": "",
        "api_key_env": "SILICONFLOW_STT_KEY",
        # resolve_key_for_role 会把环境变量解析后的 api_key 注入返回字典
        "api_key": "sk-test",
    }
    base.update(overrides)
    return base


class TestSttTranscribe:
    @pytest.mark.asyncio
    async def test_no_stt_key_raises(self):
        """llm_keys 中无可用 type=stt 条目时抛 AppException(400)。"""
        with patch("app.services.stt_service.resolve_key_for_role", return_value=None):
            with pytest.raises(AppException) as exc_info:
                await stt_service.transcribe(b"bytes", "voice.webm", "audio/webm")
        assert exc_info.value.code == "stt_no_key"
        assert exc_info.value.http_status == 400

    @pytest.mark.asyncio
    async def test_upstream_error_raises_502(self, monkeypatch):
        """上游调用失败时抛 AppException(502)。"""
        monkeypatch.setenv("SILICONFLOW_STT_KEY", "sk-test")

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("HTTP 500")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.stt_service.resolve_key_for_role", return_value=_stt_key_entry()), \
             patch("app.services.stt_service.new_client", return_value=mock_client):
            with pytest.raises(AppException) as exc_info:
                await stt_service.transcribe(b"bytes", "voice.webm", "audio/webm")
        assert exc_info.value.code == "stt_upstream_error"
        assert exc_info.value.http_status == 502

    @pytest.mark.asyncio
    async def test_success_returns_text(self, monkeypatch):
        """成功时返回识别文本，且 multipart 上传参数正确。"""
        monkeypatch.setenv("SILICONFLOW_STT_KEY", "sk-test")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"text": "你好世界"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.stt_service.resolve_key_for_role", return_value=_stt_key_entry()), \
             patch("app.services.stt_service.new_client", return_value=mock_client):
            text = await stt_service.transcribe(b"bytes", "voice.webm", "audio/webm")

        assert text == "你好世界"
        mock_client.post.assert_called_once()
        # 校验 multipart 上传参数
        args, kwargs = mock_client.post.call_args
        # URL 是第一个位置参数
        assert args[0] == "https://api.siliconflow.cn/v1/audio/transcriptions"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test"
        assert kwargs["data"]["model"] == "FunAudioLLM/SenseVoiceSmall"
        assert "file" in kwargs["files"]
