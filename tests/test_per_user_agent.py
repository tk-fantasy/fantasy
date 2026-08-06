"""Tests for per-user agent caching in Dispatcher."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_dispatcher():
    """构造一个 Dispatcher 实例，mock 掉依赖。"""
    from app.agents.dispatcher import Dispatcher

    session_store = MagicMock()
    camera_manager = MagicMock()
    camera_manager.get_state = MagicMock(return_value={})
    camera_manager.list_cameras = MagicMock(return_value=[])
    global_agent = MagicMock()
    dispatcher = Dispatcher(
        session_store=session_store,
        agent=global_agent,
        camera_manager=camera_manager,
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
             patch("app.agents.dispatcher.build_chat_agent",
                   return_value=(user_agent, (MagicMock(), MagicMock()))):
            result = await dispatcher._get_agent("user-1")

        assert result is user_agent
        assert "user-1" in dispatcher._user_agents

    @pytest.mark.asyncio
    async def test_second_call_uses_cache(self):
        dispatcher, global_agent = _make_dispatcher()
        user_agent = MagicMock()
        build_count = 0

        model_config = {"base_url": "https://api.b.com", "model": "m2", "api_key": "key-B"}

        def mock_build(tools, model_config=None):
            nonlocal build_count
            build_count += 1
            return user_agent, (MagicMock(), MagicMock())

        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
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

        def mock_build(tools, model_config=None):
            nonlocal build_count
            build_count += 1
            return user_agent, (MagicMock(), MagicMock())

        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=mock_build):
            await dispatcher._get_agent("user-1")
            await dispatcher.invalidate_user_agent("user-1")
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

        def mock_build(tools, model_config=None):
            return next(agents), (MagicMock(), MagicMock())

        with patch("app.agents.dispatcher.load_model_config_for_user", side_effect=lambda uid: next(configs)), \
             patch("app.agents.dispatcher.build_chat_agent", side_effect=mock_build):
            result_a = await dispatcher._get_agent("user-a")
            result_b = await dispatcher._get_agent("user-b")

        assert result_a is agent_a
        assert result_b is agent_b

    @pytest.mark.asyncio
    async def test_set_agent_clears_user_cache(self):
        dispatcher, global_agent = _make_dispatcher()
        # 模拟已缓存的 per-user agent
        dispatcher._user_agents["user-1"] = MagicMock()
        dispatcher._user_agents["user-2"] = MagicMock()
        assert len(dispatcher._user_agents) == 2

        new_agent = MagicMock()
        await dispatcher.set_agent(new_agent, tools=["tool1"], clients=(MagicMock(), MagicMock()))

        assert dispatcher._agent is new_agent
        assert len(dispatcher._user_agents) == 0
        assert dispatcher._tools == ["tool1"]


class TestAgentClientsLifecycle:
    """验证 agent→clients 映射：per-user 构建不误关全局 agent 的连接（#1 修复的核心）。"""

    @staticmethod
    def _make_dispatcher_with_clients():
        """构造带真实 clients mock 的 dispatcher（全局 agent 的客户端可被观测 close 调用）。"""
        from app.agents.dispatcher import Dispatcher

        global_agent = MagicMock()
        global_sync = MagicMock()
        global_async = MagicMock()
        dispatcher = Dispatcher(
            session_store=MagicMock(),
            agent=global_agent,
            camera_manager=MagicMock(),
            clients=(global_sync, global_async),
        )
        return dispatcher, global_agent, global_sync, global_async

    @pytest.mark.asyncio
    async def test_per_user_build_does_not_close_global_clients(self):
        """构建 per-user agent 时，全局 agent 的 httpx 客户端不能被关（#1 根因）。"""
        dispatcher, global_agent, global_sync, global_async = self._make_dispatcher_with_clients()
        user_agent = MagicMock()
        user_sync, user_async = MagicMock(), MagicMock()
        model_config = {"base_url": "https://b.com", "model": "m", "api_key": "k"}

        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
             patch("app.agents.dispatcher.build_chat_agent",
                   return_value=(user_agent, (user_sync, user_async))):
            await dispatcher._get_agent("user-1")

        # 全局 agent 的客户端绝不能被关
        global_sync.close.assert_not_called()
        global_async.aclose.assert_not_called()
        # per-user agent 的客户端登记进映射（按 id）
        assert id(user_agent) in dispatcher._agent_clients
        # 全局 agent 的客户端仍在映射里（没被踢出）
        assert id(global_agent) in dispatcher._agent_clients

    @pytest.mark.asyncio
    async def test_invalidate_closes_only_that_users_clients(self):
        """invalidate_user_agent 只关该用户的客户端，不动全局。"""
        dispatcher, global_agent, global_sync, global_async = self._make_dispatcher_with_clients()
        user_agent = MagicMock()
        user_sync, user_async = MagicMock(), MagicMock()
        model_config = {"base_url": "https://b.com", "model": "m", "api_key": "k"}

        with patch("app.agents.dispatcher.load_model_config_for_user", return_value=model_config), \
             patch("app.agents.dispatcher.build_chat_agent",
                   return_value=(user_agent, (user_sync, user_async))):
            await dispatcher._get_agent("user-1")
        await dispatcher.invalidate_user_agent("user-1")

        # 该用户的客户端被关
        user_sync.close.assert_called_once()
        user_async.aclose.assert_called_once()
        # 全局客户端不受影响
        global_sync.close.assert_not_called()
