"""Tests for per-user agent caching in Dispatcher."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_dispatcher():
    """构造一个 Dispatcher 实例，mock 掉依赖。"""
    from app.agents.dispatcher import Dispatcher

    session_store = MagicMock()
    camera_stream = MagicMock()
    camera_stream.get_state = MagicMock(return_value={})
    global_agent = MagicMock()
    dispatcher = Dispatcher(
        session_store=session_store,
        agent=global_agent,
        camera_stream=camera_stream,
    )
    return dispatcher, global_agent


class TestGetAgent:
    """测试 Dispatcher._get_agent per-user 缓存。"""

    @pytest.mark.asyncio
    async def test_empty_user_id_returns_global_agent(self):
        dispatcher, global_agent = _make_dispatcher()
        result = await dispatcher._get_agent("")
        assert result is global_agent

    @pytest.mark.asyncio
    async def test_user_without_config_returns_global(self):
        dispatcher, global_agent = _make_dispatcher()
        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=None):
            result = await dispatcher._get_agent("user-1")
        assert result is global_agent

    @pytest.mark.asyncio
    async def test_user_with_config_returns_per_user_agent(self):
        dispatcher, global_agent = _make_dispatcher()
        user_agent = MagicMock()

        model_config = {"base_url": "https://api.b.com", "model": "m2", "api_key": "key-B"}
        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
             patch("app.agents.dispatcher.close_agent_http_clients", new=AsyncMock()), \
             patch("app.agents.dispatcher.build_chat_agent", return_value=user_agent):
            result = await dispatcher._get_agent("user-1")

        assert result is user_agent
        assert "user-1" in dispatcher._user_agents

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self):
        dispatcher, global_agent = _make_dispatcher()
        user_agent = MagicMock()
        build_count = 0

        model_config = {"base_url": "https://api.b.com", "model": "m2", "api_key": "key-B"}

        async def mock_close():
            pass

        def mock_build(tools, model_config=None):
            nonlocal build_count
            build_count += 1
            return user_agent

        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
             patch("app.agents.dispatcher.close_agent_http_clients", new=mock_close), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=mock_build):
            await dispatcher._get_agent("user-1")
            await dispatcher._get_agent("user-1")

        assert build_count == 1  # 第二次命中缓存，不重建

    @pytest.mark.asyncio
    async def test_invalidate_clears_cache(self):
        dispatcher, global_agent = _make_dispatcher()
        user_agent = MagicMock()
        build_count = 0

        model_config = {"base_url": "https://api.b.com", "model": "m2", "api_key": "key-B"}

        async def mock_close():
            pass

        def mock_build(tools, model_config=None):
            nonlocal build_count
            build_count += 1
            return user_agent

        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
             patch("app.agents.dispatcher.close_agent_http_clients", new=mock_close), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=mock_build):
            await dispatcher._get_agent("user-1")
            dispatcher.invalidate_user_agent("user-1")
            await dispatcher._get_agent("user-1")

        assert build_count == 2  # invalidate 后重建

    @pytest.mark.asyncio
    async def test_two_users_get_different_agents(self):
        dispatcher, global_agent = _make_dispatcher()

        config_a = {"base_url": "https://a.com", "model": "ma", "api_key": "ka"}
        config_b = {"base_url": "https://b.com", "model": "mb", "api_key": "kb"}
        agent_a = MagicMock()
        agent_b = MagicMock()
        configs = iter([config_a, config_b, config_a, config_b])
        agents = iter([agent_a, agent_b, agent_a, agent_b])

        async def mock_close():
            pass

        def mock_build(tools, model_config=None):
            return next(agents)

        with patch("app.agents.dispatcher.load_model_config_for_user", side_effect=lambda uid: next(configs)), \
             patch("app.agents.dispatcher.close_agent_http_clients", new=mock_close), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=mock_build):
            result_a = await dispatcher._get_agent("user-a")
            result_b = await dispatcher._get_agent("user-b")

        assert result_a is agent_a
        assert result_b is agent_b

    def test_set_agent_clears_user_cache(self):
        dispatcher, global_agent = _make_dispatcher()
        # 模拟已缓存的 per-user agent
        dispatcher._user_agents["user-1"] = MagicMock()
        dispatcher._user_agents["user-2"] = MagicMock()
        assert len(dispatcher._user_agents) == 2

        new_agent = MagicMock()
        dispatcher.set_agent(new_agent, tools=["tool1"])

        assert dispatcher._agent is new_agent
        assert len(dispatcher._user_agents) == 0
        assert dispatcher._tools == ["tool1"]
