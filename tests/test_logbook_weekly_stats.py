"""阶段6：HA logbook 设备操作史 + 周报喂 HA 长期统计。

覆盖：
- ha_client.logbook 的 URL/参数拼装（MockTransport）
- get_device_history 聊天工具：正常返回紧凑行、无记录、无 entity_id、HA 不可用
- WeeklyReportService._ha_stats_text：显式配置实体 / 自动按 device_class 挑选 /
  温湿度 min-max-avg / 能耗差值（含重置跳过）/ HA 不可用静默空串
- generate：ha_stats 拼进 LLM 输入与 report 字段（LLM 不可用退化统计文本）
- REST /ha/logbook/{entity_id}
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.weekly_report_service import WeeklyReportService


class TestLogbookClient:
    async def test_logbook_url_and_params(self):
        from app.clients.ha_client import HomeAssistantClient
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((str(request.url), dict(request.url.params)))
            return httpx.Response(200, json=[
                {"when": "2026-09-15T22:00:00", "name": "Front Door",
                 "entity_id": "lock.front_door", "message": "locked by 张三"},
            ])

        import asyncio as _asyncio
        client = HomeAssistantClient.__new__(HomeAssistantClient)
        client._base_url = "http://ha:8123"
        client._token = "t"
        client._client_lock = _asyncio.Lock()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://ha:8123", headers={"Authorization": "Bearer t"})
        entries = await client.logbook(
            "lock.front_door", timestamp="2026-09-14T00:00:00")
        assert entries[0]["name"] == "Front Door"
        url, params = requests[0]
        assert url.split("?")[0].endswith("/api/logbook/lock.front_door")
        assert params["timestamp"] == "2026-09-14T00:00:00"
        await client._client.aclose()

    async def test_logbook_no_entity_uses_root(self):
        from app.clients.ha_client import HomeAssistantClient

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith("/api/logbook")
            return httpx.Response(200, json=[])

        import asyncio as _asyncio
        client = HomeAssistantClient.__new__(HomeAssistantClient)
        client._base_url = "http://ha:8123"
        client._token = "t"
        client._client_lock = _asyncio.Lock()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://ha:8123")
        assert await client.logbook() == []
        await client._client.aclose()


class TestDeviceHistoryTool:
    def _tool(self, client):
        from app.tools import ToolDeps, _register_ha_device_history
        tools = {}

        def _capture(tool):
            tools[tool.tool_name] = tool
            return True
        mgr = MagicMock()
        mgr.register_tool = _capture
        deps = MagicMock(spec=ToolDeps)
        deps.mcp_client_manager = mgr
        deps.ha_client_ref = [client]
        _register_ha_device_history(deps)
        return tools["get_device_history"]

    async def test_returns_compact_lines(self):
        client = MagicMock()
        client.logbook = AsyncMock(return_value=[
            {"when": "2026-09-15T08:00:00", "name": "大门", "message": "locked by 张三"},
            {"when": "2026-09-15T22:10:00", "name": "大门", "message": "unlocked by 李四"},
        ])
        tool = self._tool(client)
        ret = await tool.handler({"entity_id": "lock.door", "hours": 24}, None)
        assert ret["count"] == 2
        assert ret["entries"][0].startswith("2026-09-15 22:10 大门")  # 倒序（新→旧）
        client.logbook.assert_awaited_once()

    async def test_empty_entries(self):
        client = MagicMock()
        client.logbook = AsyncMock(return_value=[])
        tool = self._tool(client)
        ret = await tool.handler({"entity_id": "lock.door"}, None)
        assert ret["count"] == 0 and "没有记录" in ret["note"]

    async def test_missing_entity_id(self):
        tool = self._tool(MagicMock())
        ret = await tool.handler({}, None)
        assert "entity_id" in ret["error"]

    async def test_ha_unavailable(self):
        tool = self._tool(None)
        ret = await tool.handler({"entity_id": "lock.door"}, None)
        assert "不可用" in ret["error"]


def _ha_stub(states, histories: dict):
    """ha_service 桩：states 快照 + client.get_history 按 entity 返回。

    返回值按 HA /api/history 协议包装成外层列表（每实体一项、内层是状态点列表）。
    """
    client = MagicMock()

    async def _get_history(entity_id, timestamp=None, **kw):
        return [histories.get(entity_id, [])]
    client.get_history = _get_history
    ha = MagicMock()
    ha.get_states_snapshot = AsyncMock(return_value=states)
    ha._client = client
    return ha


def _points(values, name="客厅"):
    return [
        {"state": str(v), "attributes": {"friendly_name": name}}
        for v in values
    ]


class TestWeeklyHaStats:
    async def test_auto_pick_trend_and_energy(self, monkeypatch):
        svc = WeeklyReportService()
        states = [
            {"entity_id": "sensor.temp_living", "state": "22",
             "attributes": {"device_class": "temperature", "friendly_name": "客厅温度"}},
            {"entity_id": "sensor.humid_living", "state": "55",
             "attributes": {"device_class": "humidity", "friendly_name": "客厅湿度"}},
            {"entity_id": "sensor.energy_main", "state": "100.5",
             "attributes": {"device_class": "energy", "friendly_name": "总电表"}},
            {"entity_id": "sensor.junk", "state": "unavailable",
             "attributes": {"device_class": "temperature"}},
        ]
        ha = _ha_stub(states, {
            "sensor.temp_living": _points([20, 24, 28], name="客厅温度"),
            "sensor.humid_living": _points([50, 60], name="客厅湿度"),
            "sensor.energy_main": _points([100.0, 106.5], name="总电表"),
        })
        svc.set_ha_service(ha)
        monkeypatch.setattr(
            "app.services.weekly_report_service.get_config",
            lambda key, default=None: {} if key == "report.ha_stat_entities" else default)
        text = await svc._ha_stats_text()
        assert "客厅温度 本周 20.0~28.0°C（平均 24.0°C）" in text
        assert "客厅湿度 本周 50.0~60.0%" in text
        assert "总电表 本周用电 6.5 kWh" in text
        assert "unavailable" not in text

    async def test_explicit_config_wins(self, monkeypatch):
        svc = WeeklyReportService()
        ha = _ha_stub([], {"sensor.custom": _points([1, 3], name="自定义")})
        svc.set_ha_service(ha)
        monkeypatch.setattr(
            "app.services.weekly_report_service.get_config",
            lambda key, default=None:
            {"temperature": ["sensor.custom"]}
            if key == "report.ha_stat_entities" else default)
        text = await svc._ha_stats_text()
        assert "自定义 本周 1.0~3.0" in text

    async def test_energy_reset_skipped(self, monkeypatch):
        svc = WeeklyReportService()
        ha = _ha_stub([
            {"entity_id": "sensor.e", "state": "5",
             "attributes": {"device_class": "energy", "friendly_name": "电表"}},
        ], {"sensor.e": _points([100.0, 90.0], name="电表")})  # 重置（末值 < 首值）
        svc.set_ha_service(ha)
        monkeypatch.setattr(
            "app.services.weekly_report_service.get_config",
            lambda key, default=None: {"energy": ["sensor.e"]}
            if key == "report.ha_stat_entities" else default)
        assert await svc._ha_stats_text() == ""

    async def test_no_ha_returns_empty(self):
        svc = WeeklyReportService()
        assert await svc._ha_stats_text() == ""
        svc.set_ha_service(MagicMock())  # 无 _client
        assert await svc._ha_stats_text() == ""


class TestGenerateWithHaStats:
    async def test_ha_stats_flow_into_report(self, monkeypatch):
        svc = WeeklyReportService()
        svc.set_ha_service(_ha_stub([
            {"entity_id": "sensor.t", "state": "22",
             "attributes": {"device_class": "temperature", "friendly_name": "室温"}},
        ], {"sensor.t": _points([20, 26], name="室温")}))
        monkeypatch.setattr(
            "app.services.weekly_report_service.get_config",
            lambda key, default=None: {} if key == "report.ha_stat_entities" else default)

        db = MagicMock()
        db.family_events_since = AsyncMock(return_value=[
            {"created_at": 1758000000000, "kind": "automation",
             "source": "rule:x", "message": "触发"},
        ])
        db.kv_get = AsyncMock(return_value=None)
        db.kv_set = AsyncMock()
        db.family_event_add = AsyncMock()
        db.sessions_all = AsyncMock(return_value=[])
        MockDB = MagicMock()
        MockDB.get = staticmethod(lambda: db)
        monkeypatch.setattr(
            "app.services.weekly_report_service.Database", MockDB)

        captured = {}

        class LLM:
            enabled = True

            async def chat(self, messages, timeout):
                captured["content"] = messages[0]["content"]
                return "本周一切正常。"
        svc.set_llm_client(LLM())

        with patch("app.services.alert_service.alert_service.broadcast_report",
                   new=AsyncMock()) as bc:
            report = await svc.generate()
        assert report["generated"] is True
        assert "室温 本周 20.0~26.0" in report["ha_stats"]
        assert "室温 本周 20.0~26.0" in captured["content"]  # LLM 输入带上
        bc.assert_awaited_once()


class TestLogbookRoute:
    async def test_route_ok(self):
        from app.routes import ha_routes
        c = MagicMock()
        c.ha_client.logbook = AsyncMock(return_value=[{"message": "x"}])
        with patch.object(ha_routes, "get_container", return_value=c):
            res = await ha_routes.ha_logbook("lock.door", hours=24, container=c)
        assert res.data == [{"message": "x"}]
