"""Coverage-gap tests for route modules (batch B).

Covers: weather_routes, simulator_routes, scheduler_routes, scene_routes,
rule_routes, doc_routes, advanced_routes, stt_routes, home_routes,
report_routes — branches not exercised by existing test files.

Style follows the repo's existing route tests: route functions are invoked
directly with patched container/service boundaries (external HTTP, DB,
docker socket). Every test asserts real behavior (status codes, JSON
bodies, service state, written config).
"""

from __future__ import annotations

import os

import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.exceptions import AppException


def _cfg_getter(cfg):
    return lambda path, default=None: cfg.get(path, default)


async def _expect_error(coro, status: int, text: str = "") -> AppException:
    """断言路由以 AppException 失败，并校验 HTTP 状态码与消息。

    这些分支此前写的是 return ApiResponse(success=False, ...)，但 ApiResponse
    根本没有 success 字段（只有 code/message/data），Pydantic extra='ignore'
    把它静默丢掉 → 发出去的是 code='ok' + HTTP 200，前端 _unwrap 的
    `json.data ?? json` 在 data=None 时返回整个信封，失败被渲染成「✅ 已更新」。
    """
    with pytest.raises(AppException) as ei:
        await coro
    assert ei.value.http_status == status
    if text:
        assert text in ei.value.message
    return ei.value


# ---------------------------------------------------------------------------
# weather_routes
# ---------------------------------------------------------------------------

class TestWeatherRoutesGaps:
    async def test_city_route(self):
        from app.routes import weather_routes as wr

        with patch.object(wr, "city_lookup", new_callable=AsyncMock) as lookup:
            lookup.return_value = {"cities": [{"name": "上海", "id": "1"}]}
            out = await wr.weather_city(q="上海")
        assert out.code == "ok"
        assert out.data["cities"][0]["id"] == "1"
        lookup.assert_awaited_once_with("上海")

    async def test_indices_route(self):
        from app.routes import weather_routes as wr

        with patch.object(wr, "get_weather_indices", new_callable=AsyncMock) as gi:
            gi.return_value = {"location": "121.47,31.23", "indices": [{"name": "运动"}]}
            out = await wr.weather_indices(location="121.47,31.23")
        assert out.code == "ok"
        assert out.data["indices"][0]["name"] == "运动"

    async def test_get_config_masks_private_key(self):
        from app.core.config import update_config_section
        from app.routes.weather_routes import get_weather_config

        update_config_section("weather", {
            "host": "devapi.example.com", "kid": "kid-1", "sub": "pro",
            "private_key": "SECRET",
        })
        out = await get_weather_config()
        assert out.data["has_private_key"] is True
        assert "SECRET" not in str(out.data)
        assert out.data["host"] == "devapi.example.com"

    def _payload(self, **kw):
        from app.schema.api_schemas import WeatherConfigRequest

        base = {"host": "devapi.example.com", "kid": "kid-2", "sub": "pro",
                "private_key": "keydata"}
        base.update(kw)
        return WeatherConfigRequest(**base)

    async def test_set_config_probe_ok_saves(self):
        from app.core.config import get_config
        from app.routes import weather_routes as wr
        from app.services.config_probes import ProbeResult

        with patch.object(wr, "probe_weather", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=True, detail="连接成功")
            out = await wr.set_weather_config(self._payload())
        assert out.data == {"saved": True}
        probe.assert_awaited_once()
        saved = get_config("weather", {})
        assert saved["host"] == "devapi.example.com"
        assert saved["private_key"] == "keydata"

    async def test_set_config_probe_fail_rejects(self):
        from app.core.config import get_config
        from app.routes import weather_routes as wr
        from app.services.config_probes import ProbeResult

        with patch.object(wr, "probe_weather", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=False, reason="unauthorized",
                                             detail="凭证无效")
            out = await wr.set_weather_config(self._payload())
        assert out.code == "probe_failed"
        assert out.data["saved"] is False
        assert out.data["reason"] == "unauthorized"
        # 凭证没有落盘
        assert get_config("weather", {}).get("host") != "devapi.example.com" or \
            get_config("weather", {}).get("kid") != "kid-2"

    async def test_set_config_empty_key_keeps_existing_and_skips_probe(self):
        """private_key 留空 → 保留旧值且不 probe（连 JWT 都生成不了）。"""
        from app.core.config import get_config, update_config_section
        from app.routes import weather_routes as wr
        from app.services.config_probes import ProbeResult

        update_config_section("weather", {"private_key": "OLD-KEY"})
        with patch.object(wr, "probe_weather", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=True)
            out = await wr.set_weather_config(self._payload(private_key="  "))
        assert out.data == {"saved": True}
        probe.assert_awaited_once()  # 用旧 key probe
        assert get_config("weather", {})["private_key"] == "OLD-KEY"

    async def test_set_config_no_key_any_skips_probe(self):
        """无新 key 且无旧 key → 不 probe 直接保存。"""
        from app.core.config import get_config, update_config_section
        from app.routes import weather_routes as wr

        update_config_section("weather", {"private_key": ""})
        with patch.object(wr, "probe_weather", new_callable=AsyncMock) as probe:
            out = await wr.set_weather_config(self._payload(private_key=""))
        assert out.data == {"saved": True}
        probe.assert_not_awaited()
        assert get_config("weather", {})["private_key"] == ""

    async def test_test_connection_no_host(self):
        from app.routes.weather_routes import test_weather_connection

        out = await test_weather_connection()
        assert out.code == "probe_failed"
        assert out.data["connected"] is False
        assert "未配置天气 host" in out.data["detail"]

    async def test_test_connection_probe_fail(self):
        from app.core.config import update_config_section
        from app.routes import weather_routes as wr
        from app.services.config_probes import ProbeResult

        update_config_section("weather", {"host": "h.example.com", "kid": "k",
                                          "sub": "s", "private_key": "pk"})
        with patch.object(wr, "probe_weather", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=False, reason="unreachable",
                                             detail="连不上")
            out = await wr.test_weather_connection()
        assert out.code == "probe_failed"
        assert out.data["connected"] is False
        assert out.data["reason"] == "unreachable"

    async def test_test_connection_ok(self):
        from app.core.config import update_config_section
        from app.routes import weather_routes as wr
        from app.services.config_probes import ProbeResult

        update_config_section("weather", {"host": "h.example.com", "kid": "k",
                                          "sub": "s", "private_key": "pk"})
        with patch.object(wr, "probe_weather", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=True, detail="连接成功")
            out = await wr.test_weather_connection()
        assert out.code == "ok"
        assert out.data["connected"] is True


# ---------------------------------------------------------------------------
# simulator_routes
# ---------------------------------------------------------------------------

class TestSimulatorDockerInternals:
    @pytest.fixture
    def fake_sock(self, tmp_path):
        sock = tmp_path / "docker.sock"
        sock.write_text("")
        return sock

    def test_socket_available_real_path(self):
        from app.routes.simulator_routes import docker_socket_available

        # Windows 上 /var/run/docker.sock 不存在；Linux/CI 有真 socket，该断言不成立
        import os
        if os.name != "nt":
            import pytest
            pytest.skip("POSIX 上 /var/run/docker.sock 可能真实存在，无'不存在'前提")
        assert docker_socket_available() is False

    def test_socket_available_tmp_sock(self, fake_sock):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "DOCKER_SOCK", fake_sock):
            assert sr.docker_socket_available() is True

    async def test_docker_request_socket_missing(self):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "docker_socket_available", return_value=False):
            assert await sr._docker_request("GET", "/x") is None

    async def test_docker_request_success(self, fake_sock):
        from app.routes import simulator_routes as sr

        resp = httpx.Response(200, json={"State": {"Running": True}})
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.request = AsyncMock(return_value=resp)
        with patch.object(sr, "DOCKER_SOCK", fake_sock), \
             patch.object(sr.httpx, "AsyncHTTPTransport", MagicMock()), \
             patch.object(sr.httpx, "AsyncClient", MagicMock(return_value=client)):
            out = await sr._docker_request("GET", "/containers/aether/json")
        assert out is resp
        client.request.assert_awaited_once_with("GET", "http://localhost/containers/aether/json")

    async def test_docker_request_exception_returns_none(self, fake_sock):
        from app.routes import simulator_routes as sr

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.request = AsyncMock(side_effect=RuntimeError("uds broken"))
        with patch.object(sr, "DOCKER_SOCK", fake_sock), \
             patch.object(sr.httpx, "AsyncHTTPTransport", MagicMock()), \
             patch.object(sr.httpx, "AsyncClient", MagicMock(return_value=client)):
            out = await sr._docker_request("POST", "/containers/x/stop")
        assert out is None

    async def test_container_state_all_branches(self, fake_sock):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "DOCKER_SOCK", fake_sock), \
             patch.object(sr, "_docker_request", new_callable=AsyncMock) as dr:
            dr.return_value = None
            assert await sr._container_state("c") == {"available": False}

            dr.return_value = httpx.Response(404)
            out = await sr._container_state("c")
            assert out == {"available": True, "exists": False, "running": False}

            dr.return_value = httpx.Response(500)
            out = await sr._container_state("c")
            assert out["error"] == "HTTP 500"

            bad = httpx.Response(200, content=b"{not-json")
            dr.return_value = bad
            out = await sr._container_state("c")
            assert out["error"] == "bad response"

            dr.return_value = httpx.Response(200, json={"State": {"Running": True}})
            assert await sr._container_state("c") == {
                "available": True, "exists": True, "running": True}

            dr.return_value = httpx.Response(200, json={"State": {"Running": False}})
            assert (await sr._container_state("c"))["running"] is False

    async def test_container_state_socket_missing(self):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "docker_socket_available", return_value=False):
            assert await sr._container_state("c") == {"available": False}

    async def test_container_action_all_branches(self, fake_sock):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "DOCKER_SOCK", fake_sock), \
             patch.object(sr, "_docker_request", new_callable=AsyncMock) as dr:
            dr.return_value = None
            out = await sr._container_action("c", "stop")
            assert out["ok"] is False and out["error"] == "docker api 不可用"

            dr.return_value = httpx.Response(404)
            out = await sr._container_action("c", "start")
            assert out["ok"] is False and "容器不存在" in out["error"]

            dr.return_value = httpx.Response(204)
            assert await sr._container_action("c", "stop") == {
                "available": True, "ok": True}

            dr.return_value = httpx.Response(304)
            assert (await sr._container_action("c", "stop"))["ok"] is True

            dr.return_value = httpx.Response(500)
            out = await sr._container_action("c", "stop")
            assert out["ok"] is False and out["error"] == "HTTP 500"

    async def test_container_action_socket_missing(self):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "docker_socket_available", return_value=False):
            assert await sr._container_action("c", "stop") == {
                "available": False, "ok": False}

    async def test_refresh_device_views_failure_swallowed(self):
        """ha 缓存失效抛异常 → 记日志不阻塞响应。"""
        from app.routes import simulator_routes as sr

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock(
            side_effect=RuntimeError("cache boom"))
        container.catalog_refresh_fn = AsyncMock()
        await sr._refresh_device_views(container)  # 不抛
        container.catalog_refresh_fn.assert_not_awaited()

    async def test_start_socket_unavailable(self):
        from app.routes import simulator_routes as sr

        with patch.object(sr, "docker_socket_available", return_value=False):
            out = await sr.simulator_start()
        assert out.code == "unavailable"
        assert out.data == {"ok": False}

    async def test_stop_via_real_docker_request_204(self, fake_sock):
        """端到端：_docker_request 返回 204 → stop 两个容器都成功并刷新视图。"""
        from app.routes import simulator_routes as sr

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.request = AsyncMock(return_value=httpx.Response(204))
        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()
        with patch.object(sr, "DOCKER_SOCK", fake_sock), \
             patch.object(sr.httpx, "AsyncHTTPTransport", MagicMock()), \
             patch.object(sr.httpx, "AsyncClient", MagicMock(return_value=client)):
            out = await sr.simulator_stop(container=container)
        assert out.code == "ok"
        assert out.message == "已停止"
        assert out.data["ok"] is True
        container.catalog_refresh_fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# scheduler_routes
# ---------------------------------------------------------------------------

def _sched_container(tasks=None):
    cont = MagicMock()
    svc = MagicMock()
    svc.list_tasks = AsyncMock(return_value=tasks if tasks is not None else [])
    svc.add_task = AsyncMock(side_effect=lambda t: {"id": "new-1", **t})
    svc.set_enabled = AsyncMock(return_value={"id": "t1", "enabled": True})
    svc.run_now = AsyncMock(return_value={"id": "t1", "last_status": "success"})
    svc.delete_task = AsyncMock(return_value=True)
    svc.update_task = AsyncMock(return_value={"id": "t1", "name": "renamed"})
    cont.scheduler_service = svc
    return cont, svc


class TestSchedulerRoutes:
    async def test_list_tasks(self):
        from app.routes.scheduler_routes import list_scheduled_tasks

        cont, svc = _sched_container([{"id": "t1"}])
        out = await list_scheduled_tasks(container=cont)
        assert out.data == [{"id": "t1"}]

    async def test_list_tasks_not_ready(self):
        from app.routes.scheduler_routes import list_scheduled_tasks

        cont = MagicMock()
        cont.scheduler_service = None
        await _expect_error(list_scheduled_tasks(container=cont), 503, "未就绪")

    async def test_create_task_explicit_name(self):
        from app.routes.scheduler_routes import create_scheduled_task
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        cont, svc = _sched_container()
        payload = ScheduledTaskCreateRequest(
            name="晨练", schedule={"kind": "every", "every_seconds": 60},
            payload={"kind": "message", "message": "该锻炼了"})
        out = await create_scheduled_task(payload, current_user={"user_id": "u1"},
                                          container=cont)
        assert out.data["name"] == "晨练"
        assert svc.add_task.await_args.args[0]["user_id"] == "u1"

    async def test_create_task_autoname_message(self):
        from app.routes.scheduler_routes import create_scheduled_task
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        cont, svc = _sched_container()
        payload = ScheduledTaskCreateRequest(
            name="", schedule={"kind": "at", "at": "2026-07-07T08:00:00"},
            payload={"kind": "message", "message": "该起床了别赖床"})
        out = await create_scheduled_task(payload, current_user={"user_id": "u1"},
                                          container=cont)
        created = svc.add_task.await_args.args[0]
        assert created["name"].startswith("于 2026-07-07T08:00:00 执行一次 · 该起床了别赖床")

    async def test_create_task_autoname_tool_and_other(self):
        from app.routes.scheduler_routes import create_scheduled_task
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        cont, svc = _sched_container()
        sched = {"kind": "cron", "expr": "0 8 * * *"}
        await create_scheduled_task(ScheduledTaskCreateRequest(
            name="", schedule=sched,
            payload={"kind": "tool", "tool_name": "ha_devices___call_service"}),
            current_user={"user_id": "u1"}, container=cont)
        assert "ha_devices___call_service" in svc.add_task.await_args.args[0]["name"]

        await create_scheduled_task(ScheduledTaskCreateRequest(
            name="", schedule=sched, payload={"kind": "weird-kind"}),
            current_user={"user_id": "u1"}, container=cont)
        assert "weird-kind" in svc.add_task.await_args.args[0]["name"]

    async def test_create_task_not_ready(self):
        from app.routes.scheduler_routes import create_scheduled_task
        from app.schema.api_schemas import ScheduledTaskCreateRequest

        cont = MagicMock()
        cont.scheduler_service = None
        await _expect_error(
            create_scheduled_task(
                ScheduledTaskCreateRequest(name="x", schedule={}, payload={}),
                current_user={"user_id": "u1"}, container=cont),
            503, "未就绪")

    async def test_set_enabled_found_and_missing(self):
        from app.routes.scheduler_routes import set_scheduled_task_enabled
        from app.schema.api_schemas import ScheduledTaskEnabledRequest

        cont, svc = _sched_container()
        out = await set_scheduled_task_enabled("t1", ScheduledTaskEnabledRequest(enabled=False),
                                               container=cont)
        assert out.data["enabled"] is True
        svc.set_enabled.assert_awaited_with("t1", False)

        svc.set_enabled = AsyncMock(return_value=None)
        await _expect_error(
            set_scheduled_task_enabled("nope", ScheduledTaskEnabledRequest(enabled=True),
                                       container=cont),
            404, "任务不存在")

    async def test_run_now_found_missing_not_ready(self):
        from app.routes.scheduler_routes import run_scheduled_task_now

        cont, svc = _sched_container()
        out = await run_scheduled_task_now("t1", wait=True, container=cont)
        assert out.data["last_status"] == "success"
        svc.run_now.assert_awaited_with("t1", wait=True)

        svc.run_now = AsyncMock(return_value=None)
        await _expect_error(run_scheduled_task_now("nope", wait=True, container=cont),
                            404, "任务不存在")

        cont.scheduler_service = None
        await _expect_error(run_scheduled_task_now("t1", wait=True, container=cont),
                            503, "未就绪")

    async def test_delete_task(self):
        from app.routes.scheduler_routes import delete_scheduled_task

        cont, svc = _sched_container()
        out = await delete_scheduled_task("t1", container=cont)
        assert out.data == {"id": "t1"}
        svc.delete_task.assert_awaited_with("t1")

    async def test_revise_with_current(self):
        from app.routes.scheduler_routes import revise_scheduled_task
        from app.schema.api_schemas import TaskReviseRequest

        cont, svc = _sched_container()
        with patch("app.services.task_revise_service.revise_task",
                   new=AsyncMock(return_value={"task": {"name": "new"}, "summary": "改了"})) as rv:
            out = await revise_scheduled_task(
                "t1", TaskReviseRequest(instruction="改名", current={"id": "t1"}),
                container=cont)
        assert out.data["summary"] == "改了"
        rv.assert_awaited_once()

    async def test_revise_current_from_db_and_missing(self):
        from app.routes.scheduler_routes import revise_scheduled_task
        from app.schema.api_schemas import TaskReviseRequest

        cont, svc = _sched_container(tasks=[{"id": "t9", "name": "db-task"}])
        with patch("app.services.task_revise_service.revise_task",
                   new=AsyncMock(return_value={"task": {}, "summary": "s"})) as rv:
            await revise_scheduled_task(
                "t9", TaskReviseRequest(instruction="x", current={}), container=cont)
        assert rv.await_args.args[0] == {"id": "t9", "name": "db-task"}

        svc.list_tasks = AsyncMock(return_value=[])
        await _expect_error(
            revise_scheduled_task("nope", TaskReviseRequest(instruction="x", current={}),
                                  container=cont),
            404, "任务不存在")

    async def test_revise_error_and_not_ready(self):
        from app.routes.scheduler_routes import revise_scheduled_task
        from app.schema.api_schemas import TaskReviseRequest

        cont, svc = _sched_container()
        with patch("app.services.task_revise_service.revise_task",
                   new=AsyncMock(side_effect=ValueError("指令不明确"))):
            # ValueError = 指令本身的问题 → 400
            await _expect_error(
                revise_scheduled_task("t1", TaskReviseRequest(instruction="x",
                                                             current={"id": "t1"}),
                                      container=cont),
                400, "指令不明确")

        with patch("app.services.task_revise_service.revise_task",
                   new=AsyncMock(side_effect=RuntimeError("llm down"))):
            # RuntimeError = LLM 侧故障 → 502
            await _expect_error(
                revise_scheduled_task("t1", TaskReviseRequest(instruction="x",
                                                             current={"id": "t1"}),
                                      container=cont),
                502, "llm down")

        cont.scheduler_service = None
        await _expect_error(
            revise_scheduled_task("t1", TaskReviseRequest(instruction="x", current={}),
                                  container=cont),
            503, "未就绪")

    async def test_update_task(self):
        from app.routes.scheduler_routes import update_scheduled_task
        from app.schema.api_schemas import ScheduledTaskUpdateRequest

        cont, svc = _sched_container()
        out = await update_scheduled_task(
            "t1", ScheduledTaskUpdateRequest(task={"name": "renamed", "junk": 1}),
            container=cont)
        assert out.data["name"] == "renamed"
        patch_arg = svc.update_task.await_args.args[1]
        assert patch_arg == {"name": "renamed"}  # 白名单字段才透传

        svc.update_task = AsyncMock(return_value=None)
        await _expect_error(
            update_scheduled_task("nope", ScheduledTaskUpdateRequest(task={}), container=cont),
            404, "任务不存在")

    async def test_explain_task(self):
        from app.routes.scheduler_routes import explain_scheduled_task
        from app.schema.api_schemas import ExplainRequest

        cont, svc = _sched_container()
        with patch("app.services.task_revise_service.explain_task",
                   new=AsyncMock(return_value="这个任务每天 8 点触发")) as ex:
            out = await explain_scheduled_task(
                "t1", ExplainRequest(question="什么时候跑？", current={"id": "t1"}),
                container=cont)
        assert out.data["answer"].startswith("这个任务")

        svc.list_tasks = AsyncMock(return_value=[])
        await _expect_error(
            explain_scheduled_task("nope", ExplainRequest(question="q", current={}),
                                   container=cont),
            404, "任务不存在")

        with patch("app.services.task_revise_service.explain_task",
                   new=AsyncMock(side_effect=RuntimeError("llm down"))):
            await _expect_error(
                explain_scheduled_task("t1", ExplainRequest(question="q", current={"id": "t1"}),
                                       container=cont),
                502, "llm down")

    async def test_remaining_endpoints_not_ready(self):
        """svc=None 的兜底分支：enabled / delete / update / explain 全部 503。"""
        from app.routes.scheduler_routes import (
            delete_scheduled_task,
            explain_scheduled_task,
            set_scheduled_task_enabled,
            update_scheduled_task,
        )
        from app.schema.api_schemas import (ExplainRequest,
                                             ScheduledTaskEnabledRequest,
                                             ScheduledTaskUpdateRequest)

        cont = MagicMock()
        cont.scheduler_service = None
        await _expect_error(
            set_scheduled_task_enabled("t1", ScheduledTaskEnabledRequest(enabled=True),
                                       container=cont),
            503, "未就绪")
        await _expect_error(delete_scheduled_task("t1", container=cont), 503, "未就绪")
        await _expect_error(
            update_scheduled_task("t1", ScheduledTaskUpdateRequest(task={}), container=cont),
            503, "未就绪")
        await _expect_error(
            explain_scheduled_task("t1", ExplainRequest(question="q", current={}), container=cont),
            503, "未就绪")

    async def test_parse_schedule_route(self):
        from app.routes.scheduler_routes import parse_schedule
        from app.schema.api_schemas import ScheduleParseRequest

        with patch("app.services.schedule_parser_service.parse_schedule",
                   new=AsyncMock(return_value={"schedule": {"kind": "cron", "expr": "0 8 * * *"},
                                               "summary": "cron: 0 8 * * *"})) as ps:
            out = await parse_schedule(ScheduleParseRequest(phrase="每天8点"))
        assert out.data["schedule"]["expr"] == "0 8 * * *"
        ps.assert_awaited_once_with("每天8点")

        with patch("app.services.schedule_parser_service.parse_schedule",
                   new=AsyncMock(side_effect=ValueError("时间描述不明确"))):
            await _expect_error(parse_schedule(ScheduleParseRequest(phrase="大概")),
                                400, "时间描述不明确")


# ---------------------------------------------------------------------------
# scene_routes
# ---------------------------------------------------------------------------

class TestSceneRoutes:
    def _cont(self, svc):
        cont = MagicMock()
        cont.scene_service = svc
        return cont

    async def test_service_unavailable_raises_503(self):
        from app.routes.scene_routes import list_scenes

        cont = MagicMock()
        cont.scene_service = None
        with pytest.raises(AppException) as ei:
            await list_scenes(container=cont)
        assert ei.value.http_status == 503
        assert ei.value.code == "scene_unavailable"

    async def test_list_scenes(self):
        from app.routes.scene_routes import list_scenes

        svc = MagicMock()
        svc.list_scenes = AsyncMock(return_value=[{"id": "s1", "name": "观影"}])
        out = await list_scenes(container=self._cont(svc))
        assert out.data[0]["name"] == "观影"

    async def test_create_capture_and_manual(self):
        from app.routes.scene_routes import create_scene
        from app.schema.api_schemas import SceneCreateRequest

        svc = MagicMock()
        svc.capture_scene = AsyncMock(return_value={"id": "s1", "name": "当前"})
        svc.create_scene = AsyncMock(return_value={"id": "s2", "name": "观影"})
        cont = self._cont(svc)

        out = await create_scene(SceneCreateRequest(name="当前", capture=True),
                                 current_user={"user_id": "u1"}, container=cont)
        assert out.data["id"] == "s1"
        svc.capture_scene.assert_awaited_once_with("当前", user_id="u1")

        out = await create_scene(SceneCreateRequest(
            name="观影", actions=[{"domain": "light", "service": "turn_on",
                                   "entity_id": "light.x"}], id="s2"),
            current_user={"user_id": "u1"}, container=cont)
        assert out.data["id"] == "s2"
        svc.create_scene.assert_awaited_once_with(
            "观影", [{"domain": "light", "service": "turn_on", "entity_id": "light.x"}],
            user_id="u1", scene_id="s2")

    async def test_create_value_error(self):
        from app.routes.scene_routes import create_scene
        from app.schema.api_schemas import SceneCreateRequest

        svc = MagicMock()
        svc.create_scene = AsyncMock(side_effect=ValueError("场景至少需要一个动作"))
        await _expect_error(
            create_scene(SceneCreateRequest(name="空"),
                         current_user={"user_id": "u1"},
                         container=self._cont(svc)),
            400, "至少需要一个动作")

    async def test_apply_success_and_errors(self):
        from app.routes.scene_routes import apply_scene

        svc = MagicMock()
        svc.apply_scene = AsyncMock(return_value={"scene": "观影", "ok": 2, "total": 2})
        out = await apply_scene("s1", container=self._cont(svc))
        assert out.data["ok"] == 2

        svc.apply_scene = AsyncMock(side_effect=ValueError("场景不存在: s1"))
        await _expect_error(apply_scene("s1", container=self._cont(svc)), 404, "场景不存在")

        svc.apply_scene = AsyncMock(side_effect=RuntimeError("HA 服务不可用"))
        await _expect_error(apply_scene("s1", container=self._cont(svc)), 503, "HA 服务不可用")

    async def test_delete_scene(self):
        from app.routes.scene_routes import delete_scene

        svc = MagicMock()
        svc.delete_scene = AsyncMock(return_value=True)
        out = await delete_scene("s1", container=self._cont(svc))
        assert out.data == {"deleted": True}


# ---------------------------------------------------------------------------
# rule_routes
# ---------------------------------------------------------------------------

def _rule_container():
    cont = MagicMock()
    cont.rule_registry_service.list_rules = MagicMock(return_value=[])
    cont.rule_registry_service.add_rule = MagicMock(
        side_effect=lambda rule, user_id="": {"id": "r1", **rule})
    cont.rule_registry_service.get_rule = MagicMock(return_value=None)
    cont.rule_registry_service.update_rule = MagicMock(return_value={"id": "r1"})
    return cont


class TestRuleRoutesGaps:
    async def test_build_rule_empty_condition(self):
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        cont = _rule_container()
        cont.rule_service.build_rule = AsyncMock(return_value={"condition": "  "})
        await _expect_error(
            build_rule(RuleCreateRequest(text="乱写"),
                       container=cont, current_user={"user_id": "u1"}),
            400, "无法从输入中解析出有效的视觉条件")
        cont.rule_registry_service.add_rule.assert_not_called()

    async def test_build_rule_ok_injects_user(self):
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        cont = _rule_container()
        cont.rule_service.build_rule = AsyncMock(
            return_value={"condition": "有人", "name": "n"})
        out = await build_rule(RuleCreateRequest(text="有人就开灯", camera_id="cam-1"),
                               container=cont, current_user={"user_id": "u1"})
        assert out.data["id"] == "r1"
        cont.rule_service.build_rule.assert_awaited_once_with(
            "有人就开灯", user_id="u1", camera_id="cam-1")

    async def test_create_rule_empty_condition(self):
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        await _expect_error(
            create_rule(RulePayloadRequest(condition="   "),
                        container=_rule_container(),
                        current_user={"user_id": "u1"}),
            400, "规则必须包含 condition 字段")

    async def test_create_rule_ok(self):
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        cont = _rule_container()
        cont.camera_manager.list_cameras = MagicMock(
            return_value=[{"id": "cam_1", "name": "门口"}])
        # 视觉规则必须显式绑定摄像头（或显式传 ""=全部摄像头），否则 400
        out = await create_rule(RulePayloadRequest(condition="有人", camera_id="cam_1"),
                                container=cont, current_user={"user_id": "u1"})
        assert out.data["condition"] == "有人"
        added = cont.rule_registry_service.add_rule.await_args if \
            hasattr(cont.rule_registry_service.add_rule, "await_args") else None
        # add_rule 是同步 mock，校验参数
        kwargs = cont.rule_registry_service.add_rule.call_args.kwargs
        assert kwargs["user_id"] == "u1"

    async def test_revise_with_current(self):
        from app.routes.rule_routes import revise_rule
        from app.schema.api_schemas import RuleReviseRequest

        cont = _rule_container()
        cont.rule_service.revise_rule = AsyncMock(
            return_value={"rule": {"condition": "天黑"}, "summary": "改条件"})
        out = await revise_rule("r1", RuleReviseRequest(instruction="改成天黑",
                                                        current={"condition": "有人"}),
                                container=cont, current_user={"user_id": "u1"})
        assert out.data["summary"] == "改条件"

    async def test_revise_current_from_registry_and_404(self):
        from app.routes.rule_routes import revise_rule
        from app.schema.api_schemas import RuleReviseRequest

        cont = _rule_container()
        cont.rule_registry_service.get_rule = MagicMock(
            return_value={"id": "r1", "condition": "有人"})
        cont.rule_service.revise_rule = AsyncMock(return_value={"rule": {}, "summary": "s"})
        await revise_rule("r1", RuleReviseRequest(instruction="x", current={}),
                          container=cont, current_user={"user_id": "u1"})
        assert cont.rule_service.revise_rule.await_args.args[0] == {
            "id": "r1", "condition": "有人"}

        cont.rule_registry_service.get_rule = MagicMock(return_value=None)
        with pytest.raises(AppException) as ei:
            await revise_rule("nope", RuleReviseRequest(instruction="x", current={}),
                              container=cont, current_user={"user_id": "u1"})
        assert ei.value.http_status == 404

    async def test_revise_error_raises(self):
        """LLM 改失败必须以 502 抛出 —— 此前 return success=False 会被前端渲染成「✅ 已更新」。"""
        from app.routes.rule_routes import revise_rule
        from app.schema.api_schemas import RuleReviseRequest

        cont = _rule_container()
        cont.rule_service.revise_rule = AsyncMock(side_effect=RuntimeError("llm down"))
        exc = await _expect_error(
            revise_rule("r1", RuleReviseRequest(instruction="x", current={"a": 1}),
                        container=cont, current_user={"user_id": "u1"}),
            502)
        assert "修改失败" in exc.message
        assert "llm down" in exc.message

    async def test_update_rule_ok_and_errors(self):
        from app.routes.rule_routes import update_rule
        from app.schema.api_schemas import RuleUpdateRequest

        cont = _rule_container()
        out = await update_rule("r1", RuleUpdateRequest(rule={"condition": "c"}),
                                container=cont)
        assert out.data == {"id": "r1"}
        cont.rule_registry_service.update_rule.assert_called_once_with(
            "r1", {"condition": "c"})

        cont.rule_registry_service.update_rule = MagicMock(
            side_effect=RuntimeError("db broken"))
        await _expect_error(update_rule("r1", RuleUpdateRequest(rule={}), container=cont),
                            500, "保存失败")

        # AppException 透传（不被包装成 500）
        cont.rule_registry_service.update_rule = MagicMock(
            side_effect=AppException("规则不存在", code="rule_not_found", http_status=404))
        with pytest.raises(AppException) as ei:
            await update_rule("r1", RuleUpdateRequest(rule={}), container=cont)
        assert ei.value.http_status == 404
        assert ei.value.code == "rule_not_found"

    async def test_explain_ok_404_and_error(self):
        from app.routes.rule_routes import explain_rule
        from app.schema.api_schemas import ExplainRequest

        cont = _rule_container()
        cont.rule_service.explain_rule = AsyncMock(return_value="每天 8 点触发")
        out = await explain_rule("r1", ExplainRequest(question="何时?", current={"id": "r1"}),
                                 container=cont, current_user={"user_id": "u1"})
        assert out.data == {"answer": "每天 8 点触发"}

        cont.rule_registry_service.get_rule = MagicMock(return_value=None)
        with pytest.raises(AppException) as ei:
            await explain_rule("nope", ExplainRequest(question="q", current={}),
                               container=cont, current_user={"user_id": "u1"})
        assert ei.value.http_status == 404

        cont.rule_registry_service.get_rule = MagicMock(
            return_value={"id": "r1", "condition": "c"})
        cont.rule_service.explain_rule = AsyncMock(side_effect=RuntimeError("timeout"))
        exc = await _expect_error(
            explain_rule("r1", ExplainRequest(question="q", current={}),
                         container=cont, current_user={"user_id": "u1"}),
            502)
        assert "解释失败" in exc.message and "timeout" in exc.message


# ---------------------------------------------------------------------------
# doc_routes
# ---------------------------------------------------------------------------

class _Chunk:
    def __init__(self, content):
        self.choices = [MagicMock(delta=MagicMock(content=content))]


def _rag_container(search_result="上下文内容", create=None, ready=True):
    cont = MagicMock()
    rag = MagicMock()
    rag.is_ready = ready
    rag.search = AsyncMock(return_value=search_result)
    client = MagicMock()
    client.chat.completions.create = MagicMock(
        return_value=create if create is not None else iter([_Chunk("你好"), _Chunk("世界")]))
    rag.build_llm_client = AsyncMock(return_value=(client, "model-x"))
    cont.rag_service = rag
    cont.embed_client = MagicMock()
    cont.embed_client.enabled = True
    return cont, rag, client



class TestAdvancedRoutesGaps:
    async def test_get_config_password_flag(self, monkeypatch):
        from app.routes.advanced_routes import get_advanced_config

        monkeypatch.delenv("RTSP_PASSWORD", raising=False)
        out = await get_advanced_config()
        assert out.data["vision"]["has_rtsp_password"] is False
        assert out.data["web_search"]["exa"] == {"api_key": "", "has_exa_key": False}

        monkeypatch.setenv("RTSP_PASSWORD", "secret")
        out = await get_advanced_config()
        assert out.data["vision"]["has_rtsp_password"] is True

    def _req(self, **kw):
        from app.schema.api_schemas import AdvancedConfigRequest

        return AdvancedConfigRequest(**kw)

    async def test_set_config_exa_new_key_probe_ok(self):
        from app.core.config import get_config
        from app.routes import advanced_routes as ar
        from app.services.config_probes import ProbeResult

        with patch.object(ar, "probe_exa", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=True, detail="Exa 连接成功")
            out = await ar.set_advanced_config(
                self._req(web_search={"exa": {"api_key": "k" * 25}}),
                current_user={"user_id": "admin"})
        assert out.data == {"saved": True}
        assert get_config("web_search.exa.api_key") == "k" * 25

    async def test_set_config_exa_probe_fail_rejects(self):
        from app.core.config import get_config
        from app.routes import advanced_routes as ar
        from app.services.config_probes import ProbeResult

        with patch.object(ar, "probe_exa", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=False, reason="unauthorized",
                                             detail="Exa key 无效")
            out = await ar.set_advanced_config(
                self._req(web_search={"exa": {"api_key": "x" * 25}}),
                current_user={"user_id": "admin"})
        assert out.code == "probe_failed"
        assert out.data["saved"] is False
        assert get_config("web_search.exa.api_key", "") != "x" * 25

    async def test_set_config_exa_empty_keeps_old(self):
        from app.core.config import get_config, update_config_section
        from app.routes import advanced_routes as ar

        update_config_section("web_search", {"exa": {"api_key": "OLD" * 10}})
        with patch.object(ar, "probe_exa", new_callable=AsyncMock) as probe:
            out = await ar.set_advanced_config(
                self._req(web_search={"exa": {"api_key": ""}}),
                current_user={"user_id": "admin"})
        assert out.data == {"saved": True}
        probe.assert_not_awaited()  # 留空 = 不修改
        assert get_config("web_search.exa.api_key") == "OLD" * 10

    async def test_set_config_vision_and_rag_saved(self):
        from app.core.config import get_config
        from app.routes.advanced_routes import set_advanced_config
        from app.schema.api_schemas import RAGConfig, VisionConfig

        out = await set_advanced_config(
            self._req(vision=VisionConfig(jpeg_quality=55, motion_threshold=20),
                      rag=RAGConfig(retrieve_top_k=9)),
            current_user={"user_id": "admin"})
        assert out.data == {"saved": True}
        assert get_config("vision.jpeg_quality") == 55
        assert get_config("rag.retrieve_top_k") == 9

    async def test_test_exa(self):
        from app.routes import advanced_routes as ar
        from app.services.config_probes import ProbeResult

        with patch.object(ar, "probe_exa", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=False, reason="unauthorized",
                                             detail="key 无效")
            out = await ar.test_exa_connection()
        assert out.code == "probe_failed"
        assert out.data["connected"] is False

        with patch.object(ar, "probe_exa", new_callable=AsyncMock) as probe:
            probe.return_value = ProbeResult(ok=True, detail="匿名调用成功")
            out = await ar.test_exa_connection()
        assert out.code == "ok"
        assert out.data["connected"] is True

    async def test_embed_status_variants(self, monkeypatch):
        import app.core.config as cfg_mod
        from app.routes.advanced_routes import get_embed_status

        # 无 embed key、无 rag
        cont = MagicMock()
        cont.rag_service = None
        out = await get_embed_status(container=cont)
        assert out.data["configured"] is False
        assert out.data["rag_available"] is False
        assert out.data["rag_chunks"] == 0

        # 有 embed key（无 key_id）+ rag 就绪
        monkeypatch.setitem(cfg_mod.CONFIG, "llm_keys",
                            [{"type": "embed", "model": "bge-m3"}])
        monkeypatch.setitem(cfg_mod.CONFIG, "providers", {})
        rag = MagicMock()
        rag.is_ready = True
        rag.chunk_count = 42
        cont.rag_service = rag
        out = await get_embed_status(container=cont)
        assert out.data["configured"] is True
        assert out.data["model"] == "bge-m3"
        assert out.data["emoji_available"] is True
        assert out.data["rag_available"] is True
        assert out.data["rag_chunks"] == 42

        # providers.embed.key_id 已指定
        monkeypatch.setitem(cfg_mod.CONFIG, "providers", {"embed": {"key_id": "k1"}})
        out = await get_embed_status(container=cont)
        assert out.data["configured"] is True

        # rag 存在但未就绪
        rag.is_ready = False
        out = await get_embed_status(container=cont)
        assert out.data["rag_available"] is False


# ---------------------------------------------------------------------------
# stt_routes
# ---------------------------------------------------------------------------

class TestSttRoutesGaps:
    async def test_audio_too_large_rejected(self):
        from app.routes.stt_routes import transcribe
        from app.services import stt_service

        fake = MagicMock()
        fake.read = AsyncMock(return_value=b"x" * (25 * 1024 * 1024 + 1))
        fake.filename = "big.webm"
        fake.content_type = "audio/webm"
        with patch.object(stt_service, "transcribe", new_callable=AsyncMock) as tr:
            out = await transcribe(audio=fake, current_user={"user_id": "u1"})
        assert out.code == "invalid_input"
        assert "上限" in out.message
        assert out.data == {"text": ""}
        tr.assert_not_awaited()


# ---------------------------------------------------------------------------
# home_routes
# ---------------------------------------------------------------------------

def _home_db(settings):
    db = MagicMock()
    db.user_settings_all = AsyncMock(return_value=settings)
    db.user_setting_set = AsyncMock()
    return db


class TestHomeRoutesGaps:
    async def test_get_info_corrupt_json(self):
        from app.core.config import get_config
        from app.routes import home_routes as hr

        db = _home_db({"home_info": "{oops not json"})
        with patch.object(hr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await hr.get_home_info(current_user={"user_id": "u1"})
        assert out.data == {"home_name": "", "owner_name": "", "province": "",
                            "city": "", "district": ""}

    async def test_set_info_corrupt_existing_then_update(self):
        """旧 home_info 是坏 JSON → 从空开始合并新字段并镜像到全局 config。"""
        from app.core.config import get_config
        from app.routes import home_routes as hr
        from app.schema.api_schemas import HomeInfoRequest

        db = _home_db({"home_info": "{broken"})
        with patch.object(hr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await hr.set_home_info(
                HomeInfoRequest(province="广东省", city="深圳"),
                current_user={"user_id": "u1"})
        assert out.data["province"] == "广东省"
        assert out.data["city"] == "深圳"
        saved_json = db.user_setting_set.await_args.args[2]
        assert json_loads(saved_json)["city"] == "深圳"
        # 镜像到全局 config（weather_service 读的就是这份）
        assert get_config("home.city") == "深圳"
        assert get_config("home.province") == "广东省"

    async def test_set_info_preserves_existing_fields(self):
        from app.core.config import get_config
        from app.routes import home_routes as hr
        from app.schema.api_schemas import HomeInfoRequest

        db = _home_db({"home_info": json_dumps({"home_name": "我的家", "city": "上海"})})
        with patch.object(hr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await hr.set_home_info(
                HomeInfoRequest(district="浦东"),  # 空字段不覆盖
                current_user={"user_id": "u1"})
        assert out.data["home_name"] == "我的家"
        assert out.data["city"] == "上海"
        assert out.data["district"] == "浦东"


def json_loads(s):
    import json

    return json.loads(s)


def json_dumps(d):
    import json

    return json.dumps(d, ensure_ascii=False)


# ---------------------------------------------------------------------------
# report_routes
# ---------------------------------------------------------------------------

def _event(kind, created_at, **extra):
    return {"kind": kind, "created_at": created_at, "message": f"{kind}-{created_at}",
            **extra}


class TestReportRoutesGaps:
    async def test_list_events_basic_sorted_desc(self):
        from app.routes import report_routes as rr

        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=[
            _event("alert", 100), _event("task", 300), _event("alert", 200)])
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.list_events(days=7, kind="", date="")
        kinds_ts = [e["created_at"] for e in out.data]
        assert kinds_ts == [300, 200, 100]  # 最新在前

    async def test_list_events_date_not_str_normalized(self):
        from app.routes import report_routes as rr

        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=[])
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.list_events(days=7, kind="", date=None)  # 直调时 Query 对象场景
        assert out.data == []
        db.family_events_since.assert_awaited_once()

    async def test_list_events_kind_not_str_normalized(self):
        """直调函数时 kind 默认值是 Query() 对象 → 归化为 ""。"""
        from app.routes import report_routes as rr

        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=[_event("alert", 1)])
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.list_events(days=7, kind=None, date="")
        assert len(out.data) == 1  # kind 未生效过滤

    async def test_list_events_bad_date_format(self):
        from app.routes.report_routes import list_events

        out = await list_events(days=7, kind="", date="2026/01/01")
        assert out.data == []

    async def test_list_events_exact_day_window(self):
        """date=YYYY-MM-DD → [当天 00:00, 次日 00:00) 精确窗口（fake db 按 since 过滤）。"""
        from app.routes import report_routes as rr
        import datetime as dt

        db = MagicMock()
        day_start = int(dt.datetime(2026, 3, 5).astimezone().timestamp() * 1000)
        inside = day_start + 3600 * 1000
        outside_before = day_start - 1
        outside_after = day_start + 24 * 3600 * 1000 + 5
        all_events = [_event("alert", inside), _event("alert", outside_before),
                      _event("alert", outside_after)]
        # 真实 DB 按 since 过滤，fake 保持同语义
        db.family_events_since = AsyncMock(
            side_effect=lambda since: [e for e in all_events if e["created_at"] >= since])
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.list_events(days=7, kind="", date="2026-03-05")
        assert [e["created_at"] for e in out.data] == [inside]

    async def test_list_events_kind_filter(self):
        from app.routes import report_routes as rr

        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=[
            _event("alert:x", 1), _event("device_state", 2), _event("task", 3)])
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.list_events(days=7, kind="alert", date="")
        assert len(out.data) == 1
        assert out.data[0]["kind"] == "alert:x"

    async def test_list_events_truncation_by_kind_quota(self):
        """>500 条事件 → 每类保底 80 条 + 按时间补齐至 500，且整体按时间倒序。"""
        from app.routes import report_routes as rr

        events = []
        for i in range(560):
            events.append(_event("device_state", i))
        for i in range(60):
            events.append(_event("alert", 10_000 + i))
        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=events)
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.list_events(days=30, kind="", date="")
        data = out.data
        assert len(data) == 500
        # alert 类 60 条全部保留（< 80 保底）
        assert sum(1 for e in data if e["kind"] == "alert") == 60
        # 时间倒序
        ts = [e["created_at"] for e in data]
        assert ts == sorted(ts, reverse=True)

    async def test_events_stats(self):
        from app.routes import report_routes as rr

        db = MagicMock()
        db.family_events_stats = AsyncMock(return_value={"totals": {"alert": 3},
                                                         "daily": [], "top_devices": []})
        with patch.object(rr, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await rr.events_stats(days=14)
        assert out.data["totals"] == {"alert": 3}

    async def test_weekly_report_missing_service(self):
        from app.routes.report_routes import generate_weekly_report, get_weekly_report

        cont = MagicMock()
        cont.weekly_report_service = None
        # GET 无周报是正常状态（200 + data=null），不是错误
        out = await get_weekly_report(container=cont)
        assert out.data is None
        # 手动生成则必须有服务，缺了是 503
        await _expect_error(generate_weekly_report(container=cont), 503, "周报服务未就绪")

    async def test_weekly_report_latest_and_generate(self):
        from app.routes.report_routes import generate_weekly_report, get_weekly_report

        cont = MagicMock()
        svc = MagicMock()
        svc.latest_report = AsyncMock(return_value={"week": "2026-W01", "content": "…"})
        svc.generate = AsyncMock(return_value={"week": "2026-W01"})
        cont.weekly_report_service = svc
        out = await get_weekly_report(container=cont)
        assert out.data["week"] == "2026-W01"
        out = await generate_weekly_report(container=cont)
        assert out.data == {"week": "2026-W01"}

    async def test_weekly_generate_failure(self):
        from app.routes.report_routes import generate_weekly_report

        cont = MagicMock()
        svc = MagicMock()
        svc.generate = AsyncMock(side_effect=RuntimeError("llm down"))
        cont.weekly_report_service = svc
        exc = await _expect_error(generate_weekly_report(container=cont), 500)
        assert "生成失败" in exc.message
        assert "llm down" in exc.message


# ---------------------------------------------------------------------------
# 联网工具开关（web_search.enabled）：保存即重建 agent，None 不覆盖
# ---------------------------------------------------------------------------

class TestWebToolsToggle:
    @pytest.mark.asyncio
    async def test_enabled_change_triggers_agent_rebuild(self, monkeypatch):
        from app.routes import advanced_routes
        from app.schema.api_schemas import AdvancedConfigRequest, WebSearchConfig

        captured: dict = {}
        monkeypatch.setattr(advanced_routes, "update_config_section",
                            lambda section, values: captured.update({section: values}))
        # 旧状态 False → 新值 True：开关变化必须触发 rebuild
        monkeypatch.setattr(advanced_routes, "get_config",
                            lambda path, default=None: False if path == "web_search.enabled" else default)
        rebuilt = {"count": 0}

        class _FakeLock:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        import app.main as main_mod
        async def fake_rebuild():
            rebuilt["count"] += 1
        monkeypatch.setattr(main_mod, "_rebuild_lock", _FakeLock())
        monkeypatch.setattr(main_mod, "_rebuild_agent", fake_rebuild)

        payload = AdvancedConfigRequest(
            web_search=WebSearchConfig(enabled=True))
        result = await advanced_routes.set_advanced_config(
            payload, current_user={"user_id": "u1", "is_admin": 1})

        assert result.data == {"saved": True}
        assert captured["web_search"]["enabled"] is True
        assert rebuilt["count"] == 1

    @pytest.mark.asyncio
    async def test_enabled_none_does_not_touch_config(self, monkeypatch):
        from app.routes import advanced_routes
        from app.schema.api_schemas import AdvancedConfigRequest, WebSearchConfig

        captured: dict = {}
        monkeypatch.setattr(advanced_routes, "update_config_section",
                            lambda section, values: captured.update({section: values}))
        rebuilt = {"count": 0}

        class _FakeLock:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        import app.main as main_mod
        async def fake_rebuild():
            rebuilt["count"] += 1
        monkeypatch.setattr(main_mod, "_rebuild_lock", _FakeLock())
        monkeypatch.setattr(main_mod, "_rebuild_agent", fake_rebuild)

        payload = AdvancedConfigRequest(
            web_search=WebSearchConfig(enabled=None))  # 未传开关 → 不覆盖已有配置
        await advanced_routes.set_advanced_config(
            payload, current_user={"user_id": "u1", "is_admin": 1})

        assert "enabled" not in captured["web_search"]
        assert rebuilt["count"] == 0
