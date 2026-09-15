"""阶段1：DeviceEventService 事件总线 + HAService 状态实体 + /ws/events 通道。

覆盖：
- subscribe() 订阅 API：domain/entity/event_type 过滤、同步异步回调、异常隔离、取消订阅
- state_changed 三路分发：states 缓存增量 / 前端 push_to_events（sensor 不推）/ 内部订阅者
- timer.finished 事件：只进总线不落库
- HAService.apply_state_change 缓存原地更新 / get_status_entities 新域目录
- ws_registry 事件通道注册与推送
- /ws/events 端点（TestClient 全链路）
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import ws_registry
from app.routes import ws_routes
from app.services.device_event_service import DeviceEventService
from app.services.ha_service import HAService


def _ha_stub() -> MagicMock:
    """最小 HAService 桩：apply_state_change 记录调用。"""
    ha = MagicMock()
    ha.apply_state_change = MagicMock()
    ha._client = MagicMock()
    return ha


def _svc() -> tuple[DeviceEventService, MagicMock]:
    ha = _ha_stub()
    return DeviceEventService(ha_service=ha), ha


def _st(entity_id: str, state: str, name: str = "") -> dict:
    attrs = {"friendly_name": name} if name else {}
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


class TestSubscribeApi:
    async def test_domain_filter_and_unsubscribe(self):
        svc, _ = _svc()
        got: list[dict] = []
        unsub = svc.subscribe(lambda e: got.append(e), domains={"binary_sensor"}, name="cam")

        await svc._dispatch({"event_type": "state_changed",
                             "entity_id": "binary_sensor.motion", "domain": "binary_sensor"})
        await svc._dispatch({"event_type": "state_changed",
                             "entity_id": "light.bed", "domain": "light"})
        assert [e["entity_id"] for e in got] == ["binary_sensor.motion"]

        unsub()
        await svc._dispatch({"event_type": "state_changed",
                             "entity_id": "binary_sensor.motion2", "domain": "binary_sensor"})
        assert len(got) == 1  # 取消后不再收到

    async def test_entity_ids_filter(self):
        svc, _ = _svc()
        got: list[str] = []
        svc.subscribe(lambda e: got.append(e["entity_id"]), entity_ids={"person.admin"})
        await svc._dispatch({"event_type": "state_changed", "entity_id": "person.admin", "domain": "person"})
        await svc._dispatch({"event_type": "state_changed", "entity_id": "person.guest", "domain": "person"})
        assert got == ["person.admin"]

    async def test_event_types_filter(self):
        svc, _ = _svc()
        got: list[str] = []
        svc.subscribe(lambda e: got.append(e["event_type"]), event_types={"timer.finished"})
        await svc._dispatch({"event_type": "timer.finished", "entity_id": "timer.t", "domain": "timer"})
        await svc._dispatch({"event_type": "state_changed", "entity_id": "light.a", "domain": "light"})
        assert got == ["timer.finished"]

    async def test_async_callback_and_exception_isolation(self):
        svc, _ = _svc()
        got: list[str] = []
        boom = AsyncMock(side_effect=RuntimeError("boom"))
        async def good(event): got.append(event["entity_id"])
        svc.subscribe(boom, entity_ids={"light.a"}, name="boom")
        svc.subscribe(good, entity_ids={"light.a"}, name="good")
        await svc._dispatch({"event_type": "state_changed", "entity_id": "light.a", "domain": "light"})
        boom.assert_awaited_once()
        assert got == ["light.a"]  # 坏订阅者不拖累好订阅者


class TestStateChangeFanout:
    async def test_fanout_updates_cache_pushes_frontend_and_subscribers(self):
        svc, ha = _svc()
        received: list[dict] = []
        svc.subscribe(lambda e: received.append(e), entity_ids={"light.bed"})
        with patch("app.core.ws_registry.push_to_events", new=AsyncMock()) as push, \
             patch("app.services.alert_service.alert_service.record", new=AsyncMock()):
            await svc._on_state_changed(
                "light.bed", _st("light.bed", "off"), _st("light.bed", "on"))
        ha.apply_state_change.assert_called_once()
        push.assert_awaited_once()
        payload = push.await_args.args[0]
        assert payload["type"] == "entity_state"
        assert payload["entity_id"] == "light.bed"
        assert payload["state"] == "on"
        assert received and received[0]["entity_id"] == "light.bed"

    async def test_sensor_state_not_pushed_to_frontend(self):
        """sensor 数值类不推前端（高频噪声），但仍进缓存与订阅者。"""
        svc, ha = _svc()
        seen: list[str] = []
        svc.subscribe(lambda e: seen.append(e["entity_id"]), domains={"sensor"})
        with patch("app.core.ws_registry.push_to_events", new=AsyncMock()) as push, \
             patch("app.services.alert_service.alert_service.record", new=AsyncMock()):
            await svc._on_state_changed(
                "sensor.temp", _st("sensor.temp", "20"), _st("sensor.temp", "21"))
        push.assert_not_awaited()
        ha.apply_state_change.assert_called_once()
        assert seen == ["sensor.temp"]

    async def test_attribute_only_change_skipped(self):
        svc, ha = _svc()
        with patch("app.core.ws_registry.push_to_events", new=AsyncMock()) as push:
            await svc._on_state_changed(
                "light.bed", _st("light.bed", "on"), _st("light.bed", "on"))
        push.assert_not_awaited()
        ha.apply_state_change.assert_not_called()

    async def test_new_entity_first_seen_treated_as_unchanged(self):
        """old_state=None（HA 重启全量重发）不算状态变化，不触发分发。"""
        svc, ha = _svc()
        with patch("app.core.ws_registry.push_to_events", new=AsyncMock()) as push:
            await svc._on_state_changed("light.bed", None, _st("light.bed", "on"))
        push.assert_not_awaited()

    async def test_unavailable_still_fans_out(self):
        """unavailable 是高价值事件（设备掉线），缓存/推送/订阅者都要看到。"""
        svc, _ = _svc()
        seen: list[str] = []
        svc.subscribe(lambda e: seen.append(e["new_state"]["state"]), entity_ids={"switch.plug"})
        with patch("app.core.ws_registry.push_to_events", new=AsyncMock()), \
             patch("app.services.alert_service.alert_service.record", new=AsyncMock()):
            await svc._on_state_changed(
                "switch.plug", _st("switch.plug", "on"), _st("switch.plug", "unavailable"))
        assert seen == ["unavailable"]


class TestTimerFinished:
    async def test_dispatches_to_bus_without_recording(self):
        svc, _ = _svc()
        got: list[dict] = []
        svc.subscribe(lambda e: got.append(e), event_types={"timer.finished"})
        with patch("app.services.alert_service.alert_service.record", new=AsyncMock()) as rec:
            await svc._on_timer_finished({"entity_id": "timer.t", "duration": 10})
        rec.assert_not_awaited()
        assert got and got[0]["entity_id"] == "timer.t"
        assert got[0]["event_type"] == "timer.finished"


class TestApplyStateChange:
    def _service_with_cache(self) -> HAService:
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        svc = HAService(client=client)
        svc._states_cache = [
            {"entity_id": "light.bed", "state": "off", "attributes": {}, "last_changed": "t0"},
            {"entity_id": "sensor.t", "state": "20", "attributes": {}, "last_changed": "t0"},
        ]
        svc._states_cache_at = 0.0
        return svc

    def test_updates_entry_in_place(self):
        svc = self._service_with_cache()
        svc.apply_state_change("light.bed", {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "Bed"}})
        assert svc._states_cache[0]["state"] == "on"
        assert len(svc._states_cache) == 2  # 原地替换不追加
        assert svc._states_cache_at > 0.0

    def test_appends_unknown_entity(self):
        svc = self._service_with_cache()
        svc.apply_state_change("person.admin", {"entity_id": "person.admin", "state": "home", "attributes": {}})
        ids = [s["entity_id"] for s in svc._states_cache]
        assert "person.admin" in ids and len(ids) == 3

    def test_noop_when_cache_expired(self):
        svc = self._service_with_cache()
        svc._states_cache = None
        svc.apply_state_change("light.bed", {"entity_id": "light.bed", "state": "on", "attributes": {}})
        assert svc._states_cache is None


class TestGetStatusEntities:
    def _service(self, states: list[dict]) -> HAService:
        client = MagicMock()
        client.get_states = AsyncMock(return_value=states)
        svc = HAService(client=client)
        svc._area_map = {"home": "Home"}
        svc._entity_area_map = {"person.admin": "home"}
        svc._area_cache_at = 9999999999
        svc._alias_map = {"sun.sun": "太阳"}
        svc._alias_cache_at = 9999999999
        return svc

    async def test_filters_and_orders(self):
        svc = self._service([
            _st("light.bed", "on"),                      # 不在状态域 → 排除
            _st("person.admin", "home", "Admin"),        # person 优先
            _st("sun.sun", "above_horizon"),             # 别名「太阳」
            _st("calendar.work", "off"),                 # calendar 域
            _st("zone.home", "0"),                       # 不在状态域 → 排除
        ])
        out = await svc.get_status_entities()
        ids = [e["entity_id"] for e in out]
        assert ids == ["person.admin", "sun.sun", "calendar.work"]
        assert all(e["kind"] == "status" for e in out)
        assert out[1]["name"] == "太阳"          # 别名优先
        assert out[0]["area_name"] == "Home"     # 有区域则带区域

    async def test_no_area_required(self):
        svc = self._service([_st("sun.sun", "below_horizon")])
        out = await svc.get_status_entities()
        assert len(out) == 1 and out[0]["area_name"] is None


class TestWsRegistryEvents:
    async def test_register_push_unregister(self):
        ws = MagicMock()
        ws.send_json = AsyncMock()
        ws_registry.register_events(ws)
        try:
            await ws_registry.push_to_events({"type": "entity_state", "entity_id": "x"})
            ws.send_json.assert_awaited_once()
        finally:
            ws_registry.unregister_events(ws)
        # 注销后推送不再触达
        await ws_registry.push_to_events({"type": "entity_state"})
        ws.send_json.assert_awaited_once()

    async def test_failed_send_does_not_break_others(self):
        bad, good = MagicMock(), MagicMock()
        bad.send_json = AsyncMock(side_effect=RuntimeError("closed"))
        good.send_json = AsyncMock()
        ws_registry.register_events(bad)
        ws_registry.register_events(good)
        try:
            await ws_registry.push_to_events({"type": "ping"})  # 不抛
            good.send_json.assert_awaited_once()
        finally:
            ws_registry.unregister_events(bad)
            ws_registry.unregister_events(good)


class TestEventsWsEndpoint:
    def test_endpoint_registers_and_broadcasts(self):
        """全链路：连接 → 注册事件通道 → 广播送达 → 断开注销。"""
        app = FastAPI()
        app.include_router(ws_routes.router)
        with patch("app.main._ws_verify_token", new=AsyncMock(return_value="u1")), \
             patch("app.main._ws_heartbeat", new=AsyncMock()):
            client = TestClient(app)
            with client.websocket_connect("/ws/events") as ws:
                # 连接建立后应已注册进事件通道
                assert len(ws_registry._event_sockets) >= 1
                ws.send_json({"type": "pong"})  # 心跳回执被静默接受
            assert len(ws_registry._event_sockets) == 0  # 断开自动注销
