"""Tests for LangGraph agent message conversion and config loading."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.langgraph_agent import (
    session_to_langchain_messages,
    _load_model_config_from_config,
    make_post_model_hook,
    tool_call_signature,
)
from app.services.session_store import SessionState


def _mock_get_config(mock_config):
    """Helper to create a get_config side effect."""
    def side_effect(path, default=None):
        parts = path.split(".")
        val = mock_config
        for p in parts:
            if isinstance(val, dict) and p in val:
                val = val[p]
            else:
                return default
        return val
    return side_effect


class TestSessionToLangchainMessages:
    def test_empty_session(self):
        session = SessionState(session_id="test", request_id="req")
        messages = session_to_langchain_messages(session)
        assert messages == []

    def test_with_system_prompt(self):
        session = SessionState(session_id="test", request_id="req")
        messages = session_to_langchain_messages(session, system_prompt="You are helpful")
        assert len(messages) == 1
        assert isinstance(messages[0], SystemMessage)
        assert messages[0].content == "You are helpful"

    def test_user_and_assistant_messages(self):
        session = SessionState(session_id="test", request_id="req")
        session.model_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "user", "content": "How are you?"},
        ]
        messages = session_to_langchain_messages(session)
        assert len(messages) == 3
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].content == "Hello"
        assert isinstance(messages[1], AIMessage)
        assert messages[1].content == "Hi there"
        assert isinstance(messages[2], HumanMessage)
        assert messages[2].content == "How are you?"

    def test_with_system_prompt_and_messages(self):
        session = SessionState(session_id="test", request_id="req")
        session.model_messages = [{"role": "user", "content": "Hi"}]
        messages = session_to_langchain_messages(session, system_prompt="System")
        assert len(messages) == 2
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)

    def test_empty_content(self):
        session = SessionState(session_id="test", request_id="req")
        session.model_messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
        ]
        messages = session_to_langchain_messages(session)
        assert len(messages) == 2
        assert messages[0].content == ""
        assert messages[1].content == ""


class TestLoadModelConfigFromConfig:
    def test_with_key_id_found(self):
        mock_config = {
            "providers": {"chat": {"key_id": "test-key"}},
            "llm_keys": [
                {
                    "id": "test-key",
                    "base_url": "https://api.example.com/v1",
                    "model": "gpt-4",
                    "api_key_env": "TEST_API_KEY",
                }
            ],
        }
        with patch("app.core.key_resolver.get_config") as mock_get:
            mock_get.side_effect = _mock_get_config(mock_config)

            with patch.dict("os.environ", {"TEST_API_KEY": "sk-test123"}):
                config = _load_model_config_from_config()
                assert config["base_url"] == "https://api.example.com/v1"
                assert config["model"] == "gpt-4"
                assert config["api_key"] == "sk-test123"

    def test_empty_api_key_becomes_not_needed(self):
        mock_config = {
            "providers": {"chat": {"key_id": "test-key"}},
            "llm_keys": [
                {
                    "id": "test-key",
                    "base_url": "https://api.example.com/v1",
                    "model": "gpt-4",
                    "api_key_env": "EMPTY_KEY",
                }
            ],
        }
        with patch("app.core.key_resolver.get_config") as mock_get:
            mock_get.side_effect = _mock_get_config(mock_config)

            with patch.dict("os.environ", {"EMPTY_KEY": ""}, clear=False):
                config = _load_model_config_from_config()
                assert config["api_key"] == "not-needed"


class TestPostModelHook:
    """测试 post_model_hook：失败重试轮按签名剔除已成功的工具调用。"""

    def test_signature_distinguishes_args(self):
        # 同工具不同参数 → 不同签名
        sig_on = tool_call_signature("ha___call_service", {"entity_id": "light.living", "action": "turn_on"})
        sig_off = tool_call_signature("ha___call_service", {"entity_id": "light.living", "action": "turn_off"})
        assert sig_on != sig_off
        # 参数键顺序不同 → 同签名（规范化）
        sig_a = tool_call_signature("t", {"a": 1, "b": 2})
        sig_b = tool_call_signature("t", {"b": 2, "a": 1})
        assert sig_a == sig_b

    @pytest.mark.asyncio
    async def test_empty_succeeded_no_filtering(self):
        hook = make_post_model_hook()
        ai = AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {"a": 1}, "id": "c1", "type": "tool_call"}],
            id="ai-1",
        )
        result = await hook({"messages": [ai]}, {"configurable": {}})
        assert result == {}

    @pytest.mark.asyncio
    async def test_strips_successful_keeps_failed_same_name(self):
        """核心场景：同工具名不同参数（开灯成功 vs 设空调50度失败），
        重试轮剔除已成功的开灯，保留失败的设空调。"""
        hook = make_post_model_hook()
        light_on = {"name": "ha___call_service", "args": {"entity_id": "light.living", "action": "turn_on"}, "id": "c1", "type": "tool_call"}
        ac_50 = {"name": "ha___call_service", "args": {"entity_id": "climate.ac", "temperature": 50}, "id": "c2", "type": "tool_call"}
        ai = AIMessage(content="retrying", tool_calls=[light_on, ac_50], id="ai-1")
        state = {"messages": [ai]}
        succeeded = {tool_call_signature("ha___call_service", {"entity_id": "light.living", "action": "turn_on"})}
        config = {"configurable": {"succeeded_tool_calls": succeeded}}

        result = await hook(state, config)

        assert result != {}
        new_ai = result["messages"][0]
        assert new_ai.id == ai.id  # 复用 id，触发 add_messages 替换
        assert len(new_ai.tool_calls) == 1
        assert new_ai.tool_calls[0]["id"] == "c2"  # 只保留失败的空调调用

    @pytest.mark.asyncio
    async def test_all_stripped_yields_no_tool_calls(self):
        """全部命中已成功 → 返回空 tool_calls 的 AIMessage，router 据此结束本轮。"""
        hook = make_post_model_hook()
        call = {"name": "t", "args": {"a": 1}, "id": "c1", "type": "tool_call"}
        ai = AIMessage(content="done", tool_calls=[call], id="ai-1")
        succeeded = {tool_call_signature("t", {"a": 1})}
        result = await hook({"messages": [ai]}, {"configurable": {"succeeded_tool_calls": succeeded}})
        new_ai = result["messages"][0]
        assert new_ai.id == ai.id
        assert new_ai.tool_calls == []

    @pytest.mark.asyncio
    async def test_no_messages_returns_empty(self):
        hook = make_post_model_hook()
        result = await hook({}, {"configurable": {"succeeded_tool_calls": {"t::{}"}}})
        assert result == {}
