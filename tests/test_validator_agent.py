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
