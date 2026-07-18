"""Tests for ValidatorAgent retry logic."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.validator_agent import ValidatorAgent


class TestShouldRetry:
    @pytest.mark.asyncio
    async def test_empty_content_no_retry(self):
        validator = ValidatorAgent(max_retries=1)
        result = await validator.should_retry("", 0)
        assert result is False

    @pytest.mark.asyncio
    async def test_whitespace_content_no_retry(self):
        validator = ValidatorAgent(max_retries=1)
        result = await validator.should_retry("   ", 0)
        assert result is False

    @pytest.mark.asyncio
    async def test_llm_returns_true(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"need_retry": true}'
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        validator._llm = mock_llm
        
        result = await validator.should_retry("我将帮你打开灯", 0)
        assert result is True

    @pytest.mark.asyncio
    async def test_llm_returns_false(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"need_retry": false}'
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        validator._llm = mock_llm
        
        result = await validator.should_retry("已经帮你打开灯了", 0)
        assert result is False

    @pytest.mark.asyncio
    async def test_llm_exception_returns_false(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("API error"))
        validator._llm = mock_llm
        
        result = await validator.should_retry("我将帮你打开灯", 0)
        assert result is False

    @pytest.mark.asyncio
    async def test_content_truncated_to_500(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "false"
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        validator._llm = mock_llm
        
        long_content = "x" * 1000
        await validator.should_retry(long_content, 0)
        
        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        assert len(messages[1].content) <= 500


class TestBuildRetryMessage:
    def test_returns_human_message(self):
        validator = ValidatorAgent(max_retries=1)
        message = validator.build_retry_message()
        assert isinstance(message, HumanMessage)

    def test_contains_tool_call_instruction(self):
        validator = ValidatorAgent(max_retries=1)
        message = validator.build_retry_message()
        assert "tool_call" in message.content

    def test_content_not_empty(self):
        validator = ValidatorAgent(max_retries=1)
        message = validator.build_retry_message()
        assert len(message.content) > 0


class TestValidatorInit:
    def test_default_max_retries(self):
        validator = ValidatorAgent()
        assert validator._max_retries == 1

    def test_custom_max_retries(self):
        validator = ValidatorAgent(max_retries=3)
        assert validator._max_retries == 3

    def test_initial_llm_is_none(self):
        validator = ValidatorAgent()
        assert validator._llm is None


# ---------------------------------------------------------------------------
# per-user 化：should_retry 按 user_id 解析 chat key，无配置回退全局
# ---------------------------------------------------------------------------

class TestValidatorPerUser:
    """ValidatorAgent per-user：主聊天重试时 validator 与主对话用同一模型。"""

    @pytest.mark.asyncio
    async def test_should_retry_uses_per_user_llm_when_configured(self):
        """有 user_id 且用户有 per-user chat key → 构造 per-user LLM，ainvoke 走 per-user 实例。"""
        validator = ValidatorAgent(max_retries=1)
        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }
        mock_response = MagicMock()
        mock_response.content = '{"need_retry": true}'
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.agents.validator_agent.ChatOpenAI", return_value=mock_llm) as MockChat:
                with patch("app.clients.http_client.new_client"), \
                     patch("app.clients.http_client.new_sync_client"):
                    result = await validator.should_retry("我将帮你打开灯", 0, user_id="u1")

        assert result is True
        # per-user LLM 被构造（带 per-user key）
        MockChat.assert_called_once()
        _, kwargs = MockChat.call_args
        assert kwargs["api_key"] == "per-user-secret"
        assert kwargs["model"] == "per-user-model"
        # ainvoke 走 per-user 实例
        mock_llm.ainvoke.assert_awaited_once()
        # per-user LLM 被缓存
        assert "u1" in validator._user_llms

    @pytest.mark.asyncio
    async def test_should_retry_falls_back_to_global_when_no_per_user_key(self):
        """有 user_id 但用户无 per-user chat key → 回退全局 _llm。"""
        validator = ValidatorAgent(max_retries=1)
        mock_global_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"need_retry": false}'
        mock_global_llm.ainvoke = AsyncMock(return_value=mock_response)
        validator._llm = mock_global_llm

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            result = await validator.should_retry("已经打开了", 0, user_id="u-no-config")

        assert result is False
        # 全局 LLM 的 ainvoke 被调
        mock_global_llm.ainvoke.assert_awaited_once()
        # 无 per-user 配置不缓存
        assert "u-no-config" not in validator._user_llms

    @pytest.mark.asyncio
    async def test_should_retry_no_user_id_uses_global(self):
        """无 user_id → 直接走全局 _llm，不调 key 解析。"""
        validator = ValidatorAgent(max_retries=1)
        mock_global_llm = MagicMock()
        mock_response = MagicMock()
        mock_response.content = '{"need_retry": false}'
        mock_global_llm.ainvoke = AsyncMock(return_value=mock_response)
        validator._llm = mock_global_llm

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value={"api_key": "should-not-be-called"})) as mock_resolve:
            result = await validator.should_retry("已经打开了", 0, user_id="")

        assert result is False
        # 无 user_id 不调 key 解析
        mock_resolve.assert_not_awaited()
        mock_global_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_per_user_llm_cached_across_calls(self):
        """同一 user_id 多次调 should_retry 只解析一次 key，复用缓存的 LLM。"""
        validator = ValidatorAgent(max_retries=1)
        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }
        mock_response = MagicMock()
        mock_response.content = '{"need_retry": false}'
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)) as mock_resolve:
            with patch("app.agents.validator_agent.ChatOpenAI", return_value=mock_llm) as MockChat:
                with patch("app.clients.http_client.new_client"), \
                     patch("app.clients.http_client.new_sync_client"):
                    await validator.should_retry("我将帮你打开灯", 0, user_id="u1")
                    await validator.should_retry("我会帮你关灯", 0, user_id="u1")

        # key 解析只调一次（第二次命中缓存）
        assert mock_resolve.await_count == 1
        # ChatOpenAI 只构造一次
        assert MockChat.call_count == 1
        # ainvoke 调了两次（复用同一实例）
        assert mock_llm.ainvoke.await_count == 2
