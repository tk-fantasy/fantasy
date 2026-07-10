"""Tests for LLM chat client."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch, PropertyMock

import pytest

from app.clients.llm_chat_client import LlmChatClient


class TestLlmChatClient:
    def setup_method(self):
        """Create client with mocked base class."""
        with patch.object(LlmChatClient, "__init__", lambda self, role="chat": None):
            self.client = LlmChatClient()
            self.client._role = "chat"
            self.client._model = "test-model"
            self.client._enabled = True

    async def test_chat_returns_content(self):
        self.client.post_chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Hello world"}}]
        })
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=False):
            result = await self.client.chat([{"role": "user", "content": "Hi"}])
            assert result == "Hello world"

    async def test_chat_empty_choices(self):
        self.client.post_chat = AsyncMock(return_value={"choices": []})
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=False):
            result = await self.client.chat([{"role": "user", "content": "Hi"}])
            assert result == ""

    async def test_chat_with_thinking_enabled(self):
        self.client.post_chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Response"}}]
        })
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=True):
            await self.client.chat([{"role": "user", "content": "Hi"}])
            call_args = self.client.post_chat.call_args
            payload = call_args[0][0]
            assert payload["thinking"] == {"type": "enabled"}

    async def test_chat_with_thinking_disabled(self):
        self.client.post_chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Response"}}]
        })
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=False):
            await self.client.chat([{"role": "user", "content": "Hi"}])
            call_args = self.client.post_chat.call_args
            payload = call_args[0][0]
            assert "thinking" not in payload

    async def test_chat_custom_timeout(self):
        self.client.post_chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Response"}}]
        })
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=False):
            await self.client.chat([{"role": "user", "content": "Hi"}], timeout=60)
            call_args = self.client.post_chat.call_args
            assert call_args[1]["timeout"] == 60

    async def test_chat_includes_model(self):
        self.client.post_chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Response"}}]
        })
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=False):
            await self.client.chat([{"role": "user", "content": "Hi"}])
            call_args = self.client.post_chat.call_args
            payload = call_args[0][0]
            assert payload["model"] == "test-model"

    async def test_chat_stream_false(self):
        self.client.post_chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "Response"}}]
        })
        with patch.object(LlmChatClient, "thinking_enabled", new_callable=PropertyMock, return_value=False):
            await self.client.chat([{"role": "user", "content": "Hi"}])
            call_args = self.client.post_chat.call_args
            payload = call_args[0][0]
            assert payload["stream"] is False
