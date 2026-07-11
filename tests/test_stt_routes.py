"""Tests for STT routes."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSttTranscribeRoute:
    @pytest.mark.asyncio
    async def test_transcribe_returns_text(self):
        """转发音频并返回识别文本。"""
        from app.routes.stt_routes import transcribe

        mock_file = MagicMock()
        mock_file.read = AsyncMock(return_value=b"audio-bytes")
        mock_file.filename = "voice.webm"
        mock_file.content_type = "audio/webm"

        mock_current_user = {"user_id": "user-1", "username": "tester"}

        with patch("app.routes.stt_routes.stt_service.transcribe", new_callable=AsyncMock) as mock_t:
            mock_t.return_value = "你好世界"
            result = await transcribe(audio=mock_file, current_user=mock_current_user)

        assert result.code == "ok"
        assert result.data["text"] == "你好世界"
        mock_t.assert_called_once_with(b"audio-bytes", filename="voice.webm", content_type="audio/webm", user_id="user-1")

    @pytest.mark.asyncio
    async def test_transcribe_empty_audio(self):
        """空音频返回 invalid_input。"""
        from app.routes.stt_routes import transcribe

        mock_file = MagicMock()
        mock_file.read = AsyncMock(return_value=b"")

        mock_current_user = {"user_id": "user-1", "username": "tester"}

        result = await transcribe(audio=mock_file, current_user=mock_current_user)
        assert result.code == "invalid_input"
        assert result.data["text"] == ""
