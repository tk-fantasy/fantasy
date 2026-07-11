"""Tests for route modules: setup_routes, mcp_routes, scheduler_routes。

直接调路由函数（mock container），验证：
- setup_routes: setup_status / setup_ha 的配置检查与连接测试
- mcp_routes: list_mcp_servers / _is_allowed_external_mcp 白名单 / agents_status
- scheduler_routes: scheduler 未就绪守卫 / parse_schedule 转发
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.api_models import ApiResponse


def _mock_container(**overrides):
    c = MagicMock()
    c.ha_client = MagicMock()
    c.mcp_client_manager = MagicMock()
    c.mcp_client_manager._tools = {}
    c.mcp_client_manager.list_external_servers.return_value = []
    c.scheduler_service = None
    c.automation_agent_ref = [None]
    c.camera_stream = MagicMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# ===================== setup_routes =====================

class TestSetupStatus:
    @pytest.mark.asyncio
    async def test_no_llm_key_no_ha_incomplete(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers = {}
        req.cookies = {}
        req.query_params = {}

        with patch.object(setup_routes, "get_config", return_value={}), \
             patch.object(setup_routes, "extract_token_from_request", return_value=None):
            result = await setup_routes.setup_status(req, container=_mock_container())

        assert result.data["setup_complete"] is False
        assert result.data["has_llm_key"] is False
        assert result.data["ha_configured"] is False

    @pytest.mark.asyncio
    async def test_llm_key_but_no_ha_still_incomplete(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers = {}
        req.cookies = {}
        req.query_params = {}

        def fake_get(path, default=None):
            if path == "llm_keys":
                return [{"name": "k1"}]
            if path == "ha":
                return {}
            return default

        with patch.object(setup_routes, "get_config", side_effect=fake_get), \
             patch.object(setup_routes, "extract_token_from_request", return_value=None):
            result = await setup_routes.setup_status(req, container=_mock_container())

        assert result.data["has_llm_key"] is True
        assert result.data["llm_key_count"] == 1
        assert result.data["ha_configured"] is False
        assert result.data["setup_complete"] is False

    @pytest.mark.asyncio
    async def test_ha_configured_and_connected_complete(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers = {}
        req.cookies = {}
        req.query_params = {}

        container = _mock_container()
        container.ha_client.get_states = AsyncMock(return_value=[{"e": 1}, {"e": 2}])

        def fake_get(path, default=None):
            if path == "llm_keys":
                return [{"name": "k1"}]
            if path == "ha":
                return {"url": "http://ha:8123", "token": "abc"}
            return default

        with patch.object(setup_routes, "get_config", side_effect=fake_get), \
             patch.object(setup_routes, "extract_token_from_request", return_value=None):
            result = await setup_routes.setup_status(req, container=container)

        assert result.data["ha_configured"] is True
        assert result.data["ha_connected"] is True
        assert result.data["setup_complete"] is True

    @pytest.mark.asyncio
    async def test_ha_configured_but_unreachable(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers = {}
        req.cookies = {}
        req.query_params = {}

        container = _mock_container()
        container.ha_client.get_states = AsyncMock(side_effect=RuntimeError("refused"))

        def fake_get(path, default=None):
            if path == "llm_keys":
                return [{"name": "k1"}]
            if path == "ha":
                return {"url": "http://ha:8123", "token": "abc"}
            return default

        with patch.object(setup_routes, "get_config", side_effect=fake_get), \
             patch.object(setup_routes, "extract_token_from_request", return_value=None):
            result = await setup_routes.setup_status(req, container=container)

        assert result.data["ha_configured"] is True
        assert result.data["ha_connected"] is False
        # ha_configured=True + has_llm_key=True → setup_complete 仍 True
        assert result.data["setup_complete"] is True


class TestSetupHa:
    @pytest.mark.asyncio
    async def test_missing_token_returns_missing_auth(self):
        from app.routes import setup_routes

        req = MagicMock()
        body = setup_routes.HASetupRequest(url="http://ha:8123", token="tok")

        with patch.object(setup_routes, "extract_token_from_request", return_value=None):
            result = await setup_routes.setup_ha(body, req, container=_mock_container())

        assert result.code == "missing_auth"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_invalid_token(self):
        from app.routes import setup_routes
        from app.core.exceptions import AppException

        req = MagicMock()
        body = setup_routes.HASetupRequest(url="http://ha:8123", token="tok")

        with patch.object(setup_routes, "extract_token_from_request", return_value="bad"), \
             patch.object(setup_routes, "verify_token", side_effect=AppException("bad", http_status=401)):
            result = await setup_routes.setup_ha(body, req, container=_mock_container())

        assert result.code == "invalid_token"

    @pytest.mark.asyncio
    async def test_empty_url_or_token_rejected(self):
        from app.routes import setup_routes

        req = MagicMock()
        body = setup_routes.HASetupRequest(url="", token="tok")

        with patch.object(setup_routes, "extract_token_from_request", return_value="tok"), \
             patch.object(setup_routes, "verify_token", return_value={"sub": "u1"}):
            result = await setup_routes.setup_ha(body, req, container=_mock_container())

        assert result.code == "invalid_input"

    @pytest.mark.asyncio
    async def test_successful_connection(self):
        from app.routes import setup_routes

        req = MagicMock()
        body = setup_routes.HASetupRequest(url="http://ha:8123/", token="abc")

        container = _mock_container()
        container.ha_client.get_states = AsyncMock(return_value=[{"e": 1}])

        with patch.object(setup_routes, "extract_token_from_request", return_value="tok"), \
             patch.object(setup_routes, "verify_token", return_value={"sub": "u1"}), \
             patch.object(setup_routes, "update_config_section") as mock_update:
            result = await setup_routes.setup_ha(body, req, container=container)

        assert result.data["ha_connected"] is True
        assert result.data["entity_count"] == 1
        # url 是 body.url.strip()（route 不在响应里 rstrip，只 rstrip 进 ha_client._base_url）
        assert result.data["url"] == "http://ha:8123/"
        # config 写入的是 strip 后的 url
        mock_update.assert_called_once_with("ha", {"url": "http://ha:8123/", "token": "abc"})


# ===================== mcp_routes =====================

class TestListMcpServers:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_servers(self):
        from app.routes import mcp_routes

        container = _mock_container()
        result = await mcp_routes.list_mcp_servers(container=container)
        assert result.data["servers"] == []
        assert result.data["tools"] == []

    @pytest.mark.asyncio
    async def test_returns_servers_and_tools(self):
        from app.routes import mcp_routes

        container = _mock_container()
        container.mcp_client_manager.list_external_servers.return_value = [{"name": "ext"}]
        tool = MagicMock()
        tool.description = "does X"
        container.mcp_client_manager._tools = {"tool_a": tool}

        result = await mcp_routes.list_mcp_servers(container=container)
        assert len(result.data["servers"]) == 1
        assert result.data["tools"][0]["name"] == "tool_a"
        assert result.data["tools"][0]["description"] == "does X"


class TestIsAllowedExternalMcp:
    """白名单校验（RCE 防护关键）。"""

    def test_whitelisted_pair_allowed(self):
        from app.routes.mcp_routes import _is_allowed_external_mcp

        with patch("app.routes.mcp_routes.get_config", return_value=[
            {"name": "fs", "cmd": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
        ]):
            assert _is_allowed_external_mcp("fs", "npx") is True

    def test_unknown_name_rejected(self):
        from app.routes.mcp_routes import _is_allowed_external_mcp

        with patch("app.routes.mcp_routes.get_config", return_value=[
            {"name": "fs", "cmd": "npx"},
        ]):
            assert _is_allowed_external_mcp("evil", "npx") is False

    def test_wrong_cmd_rejected(self):
        """name 匹配但 cmd 不同 → 拒绝（防 cmd 篡改）。"""
        from app.routes.mcp_routes import _is_allowed_external_mcp

        with patch("app.routes.mcp_routes.get_config", return_value=[
            {"name": "fs", "cmd": "npx"},
        ]):
            assert _is_allowed_external_mcp("fs", "rm -rf /") is False

    def test_empty_whitelist_rejected(self):
        from app.routes.mcp_routes import _is_allowed_external_mcp

        with patch("app.routes.mcp_routes.get_config", return_value=[]):
            assert _is_allowed_external_mcp("any", "any") is False

    def test_none_whitelist_rejected(self):
        from app.routes.mcp_routes import _is_allowed_external_mcp

        with patch("app.routes.mcp_routes.get_config", return_value=None):
            assert _is_allowed_external_mcp("any", "any") is False


class TestAgentsStatus:
    @pytest.mark.asyncio
    async def test_not_started(self):
        from app.routes import mcp_routes

        container = _mock_container()
        container.automation_agent_ref = [None]
        result = await mcp_routes.agents_status(container=container)
        assert result.data["status"] == "not_started"

    @pytest.mark.asyncio
    async def test_running(self):
        from app.routes import mcp_routes

        agent = MagicMock()
        agent._running = True
        agent._eval_interval = 10.0
        agent._eval_count = 5
        container = _mock_container()
        container.automation_agent_ref = [agent]

        result = await mcp_routes.agents_status(container=container)
        assert result.data["automation"]["running"] is True
        assert result.data["automation"]["eval_count"] == 5


# ===================== scheduler_routes =====================

class TestSchedulerRoutesGuard:
    """scheduler_service 未就绪时所有路由返回失败（不抛）。"""

    @pytest.mark.asyncio
    async def test_list_tasks_scheduler_not_ready(self):
        from app.routes import scheduler_routes

        container = _mock_container()  # scheduler_service=None
        result = await scheduler_routes.list_scheduled_tasks(container=container)
        assert result.data is None
        assert "未就绪" in result.message

    @pytest.mark.asyncio
    async def test_create_task_scheduler_not_ready(self):
        from app.routes import scheduler_routes
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        payload = ScheduledTaskCreateRequest(
            name="t", schedule={"kind": "every", "every_seconds": 60},
            payload={"kind": "message", "message": "hi"},
        )
        container = _mock_container()
        result = await scheduler_routes.create_scheduled_task(payload, container=container)
        assert result.data is None

    @pytest.mark.asyncio
    async def test_set_enabled_scheduler_not_ready(self):
        from app.routes import scheduler_routes
        from app.schema.api_schemas import ScheduledTaskEnabledRequest

        container = _mock_container()
        result = await scheduler_routes.set_scheduled_task_enabled(
            "t1", ScheduledTaskEnabledRequest(enabled=False), container=container,
        )
        assert result.data is None

    @pytest.mark.asyncio
    async def test_run_now_scheduler_not_ready(self):
        from app.routes import scheduler_routes

        container = _mock_container()
        result = await scheduler_routes.run_scheduled_task_now("t1", container=container)
        assert result.data is None

    @pytest.mark.asyncio
    async def test_delete_task_scheduler_not_ready(self):
        from app.routes import scheduler_routes

        container = _mock_container()
        result = await scheduler_routes.delete_scheduled_task("t1", container=container)
        assert result.data is None


class TestSchedulerListTasks:
    @pytest.mark.asyncio
    async def test_list_returns_tasks(self):
        from app.routes import scheduler_routes

        container = _mock_container()
        container.scheduler_service = MagicMock()
        container.scheduler_service.list_tasks = AsyncMock(return_value=[{"id": "t1"}])

        result = await scheduler_routes.list_scheduled_tasks(container=container)
        assert result.data == [{"id": "t1"}]


class TestSchedulerCreateTask:
    @pytest.mark.asyncio
    async def test_create_with_explicit_name(self):
        from app.routes import scheduler_routes
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        container = _mock_container()
        container.scheduler_service = MagicMock()
        container.scheduler_service.add_task = AsyncMock(return_value={"id": "new"})

        payload = ScheduledTaskCreateRequest(
            name="我的任务",
            schedule={"kind": "every", "every_seconds": 60},
            payload={"kind": "message", "message": "hi"},
        )
        result = await scheduler_routes.create_scheduled_task(payload, container=container)
        assert result.data == {"id": "new"}
        # add_task 收到 name="我的任务"
        call_args = container.scheduler_service.add_task.call_args[0][0]
        assert call_args["name"] == "我的任务"

    @pytest.mark.asyncio
    async def test_create_auto_name_when_empty(self):
        """name 为空时自动生成（schedule 摘要 + payload 摘要）。"""
        from app.routes import scheduler_routes
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        container = _mock_container()
        container.scheduler_service = MagicMock()
        container.scheduler_service.add_task = AsyncMock(return_value={"id": "new"})

        payload = ScheduledTaskCreateRequest(
            name="",
            schedule={"kind": "every", "every_seconds": 60},
            payload={"kind": "message", "message": "提醒内容"},
        )
        result = await scheduler_routes.create_scheduled_task(payload, container=container)
        call_args = container.scheduler_service.add_task.call_args[0][0]
        # 自动生成的 name 非空
        assert call_args["name"]
        assert "提醒内容" in call_args["name"] or "每" in call_args["name"]
