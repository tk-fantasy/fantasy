"""Tests for LlmBaseClient — unified OpenAI format."""
from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest

from app.clients.llm_base_client import LlmBaseClient


class TestHeaders:
    def test_auth_header_when_api_key_set(self):
        client = LlmBaseClient(role="chat")
        client._api_key = "test-key-123"
        headers = client._headers()
        assert headers["Authorization"] == "Bearer test-key-123"
        assert headers["Content-Type"] == "application/json"

    def test_no_auth_header_when_no_key(self):
        client = LlmBaseClient(role="chat")
        client._api_key = ""
        headers = client._headers()
        assert "Authorization" not in headers
        assert headers["Content-Type"] == "application/json"


class TestPostChat:
    @patch.object(LlmBaseClient, "post_json", new_callable=AsyncMock)
    async def test_sends_payload_directly(self, mock_post_json):
        mock_post_json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        client = LlmBaseClient(role="chat")
        payload = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        }
        result = await client.post_chat(payload, timeout=10)
        mock_post_json.assert_called_once_with(client._chat_path, payload, timeout=10)
        assert result == {"choices": [{"message": {"content": "ok"}}]}


class TestPostEmbedding:
    @patch.object(LlmBaseClient, "post_json", new_callable=AsyncMock)
    async def test_translates_and_unpacks(self, mock_post_json):
        mock_post_json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3], "index": 0}]
        }
        client = LlmBaseClient(role="embed")
        payload = {"model": "embed-model", "prompt": "hello"}
        result = await client.post_embedding(payload, timeout=10)
        # Should translate prompt -> input
        call_args = mock_post_json.call_args
        sent_payload = call_args[0][1]
        assert sent_payload == {"model": "embed-model", "input": "hello"}
        # Should return {"embedding": [...]}
        assert result == {"embedding": [0.1, 0.2, 0.3]}

    @patch.object(LlmBaseClient, "post_json", new_callable=AsyncMock)
    async def test_raises_on_bad_response(self, mock_post_json):
        mock_post_json.return_value = {"data": []}
        client = LlmBaseClient(role="embed")
        payload = {"model": "embed-model", "prompt": "hello"}
        from app.core.exceptions import ModelServiceException
        with pytest.raises(ModelServiceException):
            await client.post_embedding(payload)


class TestPostEmbeddingsBatch:
    @patch.object(LlmBaseClient, "post_json", new_callable=AsyncMock)
    async def test_batch_sends_array_input(self, mock_post_json):
        mock_post_json.return_value = {
            "data": [
                {"embedding": [0.1, 0.2], "index": 0},
                {"embedding": [0.3, 0.4], "index": 1},
                {"embedding": [0.5, 0.6], "index": 2},
            ]
        }
        client = LlmBaseClient(role="embed")
        client._model = "embed-model"
        result = await client.post_embeddings_batch(["a", "b", "c"], timeout=10)
        call_args = mock_post_json.call_args
        sent_payload = call_args[0][1]
        assert sent_payload == {"model": "embed-model", "input": ["a", "b", "c"]}
        assert result == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]

    @patch.object(LlmBaseClient, "post_json", new_callable=AsyncMock)
    async def test_batch_raises_on_count_mismatch(self, mock_post_json):
        mock_post_json.return_value = {
            "data": [{"embedding": [0.1, 0.2], "index": 0}]
        }
        client = LlmBaseClient(role="embed")
        client._model = "embed-model"
        from app.core.exceptions import ModelServiceException
        with pytest.raises(ModelServiceException):
            await client.post_embeddings_batch(["a", "b", "c"])

    @patch.object(LlmBaseClient, "post_json", new_callable=AsyncMock)
    async def test_batch_sorts_by_index(self, mock_post_json):
        # API 返回乱序，应按 index 排列回输入顺序
        mock_post_json.return_value = {
            "data": [
                {"embedding": [0.5, 0.6], "index": 2},
                {"embedding": [0.1, 0.2], "index": 0},
                {"embedding": [0.3, 0.4], "index": 1},
            ]
        }
        client = LlmBaseClient(role="embed")
        client._model = "embed-model"
        result = await client.post_embeddings_batch(["a", "b", "c"])
        assert result == [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
