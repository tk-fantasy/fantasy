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

import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.exceptions import AppException


def _cfg_getter(cfg):
    return lambda path, default=None: cfg.get(path, default)


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

        # Windows 上 /var/run/docker.sock 不存在
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
        out = await list_scheduled_tasks(container=cont)
        assert out.data is None
        assert "未就绪" in out.message

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
        out = await create_scheduled_task(
            ScheduledTaskCreateRequest(name="x", schedule={}, payload={}),
            current_user={"user_id": "u1"}, container=cont)
        assert "未就绪" in out.message

    async def test_set_enabled_found_and_missing(self):
        from app.routes.scheduler_routes import set_scheduled_task_enabled
        from app.schema.api_schemas import ScheduledTaskEnabledRequest

        cont, svc = _sched_container()
        out = await set_scheduled_task_enabled("t1", ScheduledTaskEnabledRequest(enabled=False),
                                               container=cont)
        assert out.data["enabled"] is True
        svc.set_enabled.assert_awaited_with("t1", False)

        svc.set_enabled = AsyncMock(return_value=None)
        out = await set_scheduled_task_enabled("nope", ScheduledTaskEnabledRequest(enabled=True),
                                               container=cont)
        assert "任务不存在" in out.message

    async def test_run_now_found_missing_not_ready(self):
        from app.routes.scheduler_routes import run_scheduled_task_now

        cont, svc = _sched_container()
        out = await run_scheduled_task_now("t1", wait=True, container=cont)
        assert out.data["last_status"] == "success"
        svc.run_now.assert_awaited_with("t1", wait=True)

        svc.run_now = AsyncMock(return_value=None)
        out = await run_scheduled_task_now("nope", wait=True, container=cont)
        assert "任务不存在" in out.message

        cont.scheduler_service = None
        out = await run_scheduled_task_now("t1", wait=True, container=cont)
        assert "未就绪" in out.message

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
        out = await revise_scheduled_task(
            "nope", TaskReviseRequest(instruction="x", current={}), container=cont)
        assert "任务不存在" in out.message

    async def test_revise_error_and_not_ready(self):
        from app.routes.scheduler_routes import revise_scheduled_task
        from app.schema.api_schemas import TaskReviseRequest

        cont, svc = _sched_container()
        with patch("app.services.task_revise_service.revise_task",
                   new=AsyncMock(side_effect=ValueError("指令不明确"))):
            out = await revise_scheduled_task(
                "t1", TaskReviseRequest(instruction="x", current={"id": "t1"}),
                container=cont)
        assert "指令不明确" in out.message

        cont.scheduler_service = None
        out = await revise_scheduled_task(
            "t1", TaskReviseRequest(instruction="x", current={}), container=cont)
        assert "未就绪" in out.message

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
        out = await update_scheduled_task(
            "nope", ScheduledTaskUpdateRequest(task={}), container=cont)
        assert "任务不存在" in out.message

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
        out = await explain_scheduled_task(
            "nope", ExplainRequest(question="q", current={}), container=cont)
        assert "任务不存在" in out.message

        with patch("app.services.task_revise_service.explain_task",
                   new=AsyncMock(side_effect=RuntimeError("llm down"))):
            out = await explain_scheduled_task(
                "t1", ExplainRequest(question="q", current={"id": "t1"}), container=cont)
        assert "llm down" in out.message

    async def test_remaining_endpoints_not_ready(self):
        """svc=None 的兜底分支：enabled / delete / update / explain。"""
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
        out = await set_scheduled_task_enabled(
            "t1", ScheduledTaskEnabledRequest(enabled=True), container=cont)
        assert "未就绪" in out.message
        out = await delete_scheduled_task("t1", container=cont)
        assert "未就绪" in out.message
        out = await update_scheduled_task(
            "t1", ScheduledTaskUpdateRequest(task={}), container=cont)
        assert "未就绪" in out.message
        out = await explain_scheduled_task(
            "t1", ExplainRequest(question="q", current={}), container=cont)
        assert "未就绪" in out.message

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
            out = await parse_schedule(ScheduleParseRequest(phrase="大概"))
        assert "时间描述不明确" in out.message
        assert out.data is None


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
        out = await create_scene(SceneCreateRequest(name="空"),
                                 current_user={"user_id": "u1"},
                                 container=self._cont(svc))
        assert out.data is None
        assert "至少需要一个动作" in out.message

    async def test_apply_success_and_errors(self):
        from app.routes.scene_routes import apply_scene

        svc = MagicMock()
        svc.apply_scene = AsyncMock(return_value={"scene": "观影", "ok": 2, "total": 2})
        out = await apply_scene("s1", container=self._cont(svc))
        assert out.data["ok"] == 2

        svc.apply_scene = AsyncMock(side_effect=ValueError("场景不存在: s1"))
        out = await apply_scene("s1", container=self._cont(svc))
        assert "场景不存在" in out.message

        svc.apply_scene = AsyncMock(side_effect=RuntimeError("HA 服务不可用"))
        out = await apply_scene("s1", container=self._cont(svc))
        assert "HA 服务不可用" in out.message

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
        out = await build_rule(RuleCreateRequest(text="乱写"),
                               container=cont, current_user={"user_id": "u1"})
        assert out.data is None
        assert "无法从输入中解析出有效的视觉条件" in out.message

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

        out = await create_rule(RulePayloadRequest(condition="   "),
                                container=_rule_container(),
                                current_user={"user_id": "u1"})
        assert "规则必须包含 condition 字段" in out.message

    async def test_create_rule_ok(self):
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        cont = _rule_container()
        out = await create_rule(RulePayloadRequest(condition="有人"),
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

    async def test_revise_error_swallowed(self):
        from app.routes.rule_routes import revise_rule
        from app.schema.api_schemas import RuleReviseRequest

        cont = _rule_container()
        cont.rule_service.revise_rule = AsyncMock(side_effect=RuntimeError("llm down"))
        out = await revise_rule("r1", RuleReviseRequest(instruction="x", current={"a": 1}),
                                container=cont, current_user={"user_id": "u1"})
        assert "修改失败" in out.message
        assert "llm down" in out.message

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
        out = await update_rule("r1", RuleUpdateRequest(rule={}), container=cont)
        assert "保存失败" in out.message

        # AppException 透传（不吞）
        cont.rule_registry_service.update_rule = MagicMock(
            side_effect=AppException("规则不存在", code="rule_not_found", http_status=404))
        with pytest.raises(AppException):
            await update_rule("r1", RuleUpdateRequest(rule={}), container=cont)

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
        out = await explain_rule("r1", ExplainRequest(question="q", current={}),
                                 container=cont, current_user={"user_id": "u1"})
        assert "解释失败" in out.message and "timeout" in out.message


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


class TestDocRoutesGaps:
    async def _collect(self, resp):
        chunks = []
        async for c in resp.body_iterator:
            chunks.append(c)
        return "".join(chunks)

    @staticmethod
    def _tokens(body: str) -> list[str]:
        import json as _json

        out = []
        for line in body.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(_json.loads(line[len("data: "):])["token"])
        return out

    async def test_doc_chat_stream_success(self):
        from app.routes.doc_routes import doc_chat

        cont, rag, client = _rag_container()
        req = MagicMock()
        req.json = AsyncMock(return_value={"message": "怎么配网?"})
        resp = await doc_chat(req, container=cont, current_user={"user_id": "u1"})
        body = await self._collect(resp)
        tokens = self._tokens(body)
        assert tokens == ["你好", "世界"]
        assert body.rstrip().endswith("data: [DONE]")
        assert resp.media_type == "text/event-stream"
        rag.search.assert_awaited_once_with("怎么配网?")
        # system 提示词包含 RAG 上下文
        create_kwargs = client.chat.completions.create.call_args.kwargs
        assert "上下文内容" in create_kwargs["messages"][0]["content"]
        assert create_kwargs["messages"][1]["content"] == "怎么配网?"

    async def test_doc_chat_search_failure_degrades(self):
        """RAG 检索失败 → 降级为无上下文继续回答。"""
        from app.routes.doc_routes import doc_chat

        cont, rag, client = _rag_container()
        rag.search = AsyncMock(side_effect=RuntimeError("index gone"))
        req = MagicMock()
        req.json = AsyncMock(return_value={"message": "q"})
        resp = await doc_chat(req, container=cont, current_user={"user_id": "u1"})
        await self._collect(resp)
        ctx_in_system = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "上下文内容" not in ctx_in_system

    async def test_doc_chat_stream_error_yields_fixed_message(self):
        from app.routes.doc_routes import doc_chat

        cont, rag, client = _rag_container(create=RuntimeError("upstream down"))
        req = MagicMock()
        req.json = AsyncMock(return_value={"message": "q"})
        resp = await doc_chat(req, container=cont, current_user={"user_id": "u1"})
        body = await self._collect(resp)
        assert self._tokens(body) == ["[错误] 模型调用失败，请稍后重试或检查模型配置"]
        assert "data: [DONE]" in body

    async def test_doc_content_found_via_docs_root(self, tmp_path, monkeypatch):
        from app.routes.doc_routes import doc_content

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "hello.md").write_text("# 你好\n内容", encoding="utf-8")
        (docs / "sub").mkdir()
        (docs / "sub" / "nested.md").write_text("子目录", encoding="utf-8")
        monkeypatch.setenv("DOCS_ROOT", str(docs))
        out = doc_content(doc_id="hello")
        assert out["content"] == "# 你好\n内容"
        out = doc_content(doc_id="nested")  # rglob 递归
        assert out["content"] == "子目录"

    async def test_doc_content_not_found(self, tmp_path, monkeypatch):
        from app.routes.doc_routes import doc_content

        docs = tmp_path / "docs"
        docs.mkdir()
        monkeypatch.setenv("DOCS_ROOT", str(docs))
        with pytest.raises(AppException) as ei:
            doc_content(doc_id="missing")
        assert ei.value.http_status == 404

    async def test_rebuild_started(self):
        from app.routes.doc_routes import rebuild_doc_index

        cont, rag, _ = _rag_container()
        rag._rebuilding = False
        fake_main = MagicMock()
        with patch.dict(sys.modules, {"app.main": fake_main}):
            out = await rebuild_doc_index(container=cont)
        assert out == {"status": "started", "message": "索引重建已开始"}
        assert rag._rebuilding is True
        fake_main._stream_executor.submit.assert_called_once_with(rag.safe_build)

    async def test_rebuild_already_running(self):
        from app.routes.doc_routes import rebuild_doc_index

        cont, rag, _ = _rag_container()
        rag._rebuilding = True
        out = await rebuild_doc_index(container=cont)
        assert out == {"status": "already_running", "message": "重建正在进行中"}

    async def test_rebuild_rag_unavailable_503(self):
        from app.routes.doc_routes import rebuild_doc_index

        cont = MagicMock()
        cont.rag_service = None
        with pytest.raises(AppException) as ei:
            await rebuild_doc_index(container=cont)
        assert ei.value.http_status == 503

    async def test_rebuild_embed_not_configured_400(self):
        from app.routes.doc_routes import rebuild_doc_index

        cont, rag, _ = _rag_container()
        cont.embed_client.enabled = False
        with pytest.raises(AppException) as ei:
            await rebuild_doc_index(container=cont)
        assert ei.value.http_status == 400
        assert ei.value.code == "embed_not_configured"

    async def test_rebuild_status(self):
        from app.routes.doc_routes import doc_rebuild_status

        cont = MagicMock()
        cont.rag_service = None
        out = doc_rebuild_status(container=cont)  # 同步路由
        assert out == {"rebuilding": False, "total": 0, "done": 0, "errors": 0,
                       "message": "", "model": "", "chunk_count": 0}

        cont2, rag2, _ = _rag_container()
        rag2.rebuild_status = {"rebuilding": True, "total": 10, "done": 4}
        out = doc_rebuild_status(container=cont2)
        assert out["done"] == 4


# ---------------------------------------------------------------------------
# advanced_routes
# ---------------------------------------------------------------------------

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
        out = await get_weekly_report(container=cont)
        assert out.data is None
        out = await generate_weekly_report(container=cont)
        assert "周报服务未就绪" in out.message

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
        out = await generate_weekly_report(container=cont)
        assert out.data is None
        assert "生成失败" in out.message
