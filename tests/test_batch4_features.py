"""第 4 批新功能回归测试：告警服务 / 场景服务 / 周报。

重点验证解耦约束：notifier 反向注册、告警路径异常不影响主流程、
冷却与恢复语义、场景动作白名单化。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class FakeDB:
    def __init__(self):
        self.events: list[dict] = []
        self.scenes: dict[str, dict] = {}

    async def family_event_add(self, kind, source="", message=""):
        self.events.append({"kind": kind, "source": source, "message": message})

    async def family_events_since(self, since_ms, kinds=None):
        out = [e for e in self.events if (not kinds or e["kind"] in kinds)]
        for i, e in enumerate(out):
            e.setdefault("id", i)
            e.setdefault("created_at", since_ms + 1)
        return out

    async def scenes_all(self):
        return list(self.scenes.values())

    async def scenes_get(self, scene_id):
        return self.scenes.get(scene_id)

    async def scenes_upsert(self, scene_id, name, actions, user_id=""):
        self.scenes[scene_id] = {"id": scene_id, "name": name,
                                 "actions": actions, "user_id": user_id}

    async def scenes_delete(self, scene_id):
        return self.scenes.pop(scene_id, None) is not None


# ---------------------------------------------------------------------------
# AlertService
# ---------------------------------------------------------------------------

class TestAlertService:
    @pytest.fixture
    def svc(self):
        from app.services.alert_service import AlertService
        s = AlertService()
        with patch.object(s, "_record", new=AsyncMock()):
            yield s

    @pytest.mark.asyncio
    async def test_notify_dispatches_to_notifier(self, svc):
        received = []

        async def notifier(message, level):
            received.append((message, level))

        svc.register_notifier("test", notifier)
        await svc.notify("camera:cam1", "摄像头离线")
        assert len(received) == 1
        assert "摄像头离线" in received[0][0]

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_repeat(self, svc):
        received = []

        async def notifier(message, level):
            received.append(message)

        svc.register_notifier("test", notifier)
        await svc.notify("camera:cam1", "离线")
        await svc.notify("camera:cam1", "离线")  # 冷却期内，被吞
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_resolve_only_after_active_alert(self, svc):
        received = []

        async def notifier(message, level):
            received.append(message)

        svc.register_notifier("test", notifier)
        # 无 active 告警时 resolve 是 no-op
        await svc.resolve("camera:cam1")
        assert received == []
        await svc.notify("camera:cam1", "离线")
        await svc.resolve("camera:cam1", "摄像头恢复")
        assert len(received) == 2
        assert "恢复" in received[1]

    @pytest.mark.asyncio
    async def test_broken_notifier_does_not_break_notify(self, svc):
        async def bad_notifier(message, level):
            raise RuntimeError("boom")

        async def good_notifier(message, level):
            pass

        svc.register_notifier("bad", bad_notifier)
        svc.register_notifier("good", good_notifier)
        await svc.notify("x", "测试")  # 不应抛异常
        # bad 的异常在派生 task 里，主流程无感

    @pytest.mark.asyncio
    async def test_disabled_by_config(self, svc):
        received = []

        async def notifier(message, level):
            received.append(message)

        svc.register_notifier("t", notifier)
        with patch("app.services.alert_service.get_config", return_value=False):
            await svc.notify("x", "测试")
        assert received == []


# ---------------------------------------------------------------------------
# SceneService
# ---------------------------------------------------------------------------

class TestSceneService:
    def _svc(self, db, ha_client=None):
        from app.services.scene_service import SceneService
        return SceneService(ha_client=ha_client or MagicMock()), db

    @pytest.mark.asyncio
    async def test_create_and_apply(self):
        svc, db = self._svc(FakeDB())
        with patch("app.services.scene_service.Database") as db_cls:
            db_cls.get.return_value = db
            created = await svc.create_scene("观影", [
                {"domain": "light", "service": "turn_off", "entity_id": "light.ke_ting"},
                {"domain": "cover", "service": "close_cover", "entity_id": "cover.ke_ting"},
            ])
            assert created["name"] == "观影"

            calls = []

            async def fake_call(domain, service, entity_id, data=None):
                calls.append((domain, service, entity_id))

            svc._ha_client_ref[0] = SimpleNamespace(call_service=fake_call)
            result = await svc.apply_scene(created["id"])
        assert result["ok"] == 2 and result["total"] == 2
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_apply_continues_on_single_failure(self):
        svc, db = self._svc(FakeDB())
        with patch("app.services.scene_service.Database") as db_cls:
            db_cls.get.return_value = db
            await svc.create_scene("混合", [
                {"domain": "light", "service": "turn_on", "entity_id": "light.ok"},
                {"domain": "light", "service": "turn_on", "entity_id": "light.bad"},
            ])

            async def fake_call(domain, service, entity_id, data=None):
                if entity_id == "light.bad":
                    raise RuntimeError("HA error")

            svc._ha_client_ref[0] = SimpleNamespace(call_service=fake_call)
            result = await svc.apply_scene(list(db.scenes.keys())[0])
        assert result["ok"] == 1 and result["total"] == 2

    @pytest.mark.asyncio
    async def test_sanitize_rejects_invalid_actions(self):
        svc, db = self._svc(FakeDB())
        with patch("app.services.scene_service.Database") as db_cls:
            db_cls.get.return_value = db
            with pytest.raises(ValueError):
                await svc.create_scene("空", [
                    {"domain": "", "service": "x", "entity_id": "a.b"},   # domain 空
                    {"nope": 1},                                        # 缺字段
                ])

    @pytest.mark.asyncio
    async def test_capture_from_current_state(self):
        svc, db = self._svc(FakeDB())
        ha_service = SimpleNamespace(get_all_devices=AsyncMock(return_value=[
            {"entity_id": "light.ke_ting", "state": "on",
             "attributes": {"brightness": 180}},
            {"entity_id": "cover.wo_wo", "state": "open",
             "attributes": {"current_position": 60}},
            {"entity_id": "sensor.temp", "state": "25", "attributes": {}},  # 不可捕获
        ]))
        svc._ha_service_ref[0] = ha_service
        with patch("app.services.scene_service.Database") as db_cls:
            db_cls.get.return_value = db
            scene = await svc.capture_scene("当前")
        actions = {a["entity_id"]: a for a in scene["actions"]}
        assert "light.ke_ting" in actions
        assert actions["light.ke_ting"]["data"].get("brightness") == 180
        assert actions["cover.wo_wo"]["service"] == "set_cover_position"
        assert "sensor.temp" not in actions  # 非 capturable domain 被跳过


# ---------------------------------------------------------------------------
# WeeklyReportService
# ---------------------------------------------------------------------------

class TestWeeklyReport:
    @pytest.mark.asyncio
    async def test_generate_with_no_events_skips(self):
        from app.services.weekly_report_service import WeeklyReportService
        svc = WeeklyReportService(llm_chat_client=None)
        db = FakeDB()
        with patch("app.services.weekly_report_service.Database") as db_cls:
            db_cls.get.return_value = db
            result = await svc.generate()
        assert result["generated"] is False

    @pytest.mark.asyncio
    async def test_generate_fallback_stats_without_llm(self):
        from app.services.weekly_report_service import WeeklyReportService
        svc = WeeklyReportService(llm_chat_client=None)
        db = FakeDB()
        db.events = [
            {"kind": "automation", "source": "rule:r1", "message": "x"},
            {"kind": "automation", "source": "rule:r1", "message": "x"},
            {"kind": "task_success", "source": "scheduler:t1", "message": "y"},
            {"kind": "alert", "source": "camera:c1", "message": "离线"},
            {"kind": "alert_resolved", "source": "camera:c1", "message": "恢复"},
        ]
        db.kv = {}

        async def kv_get(key):
            return db.kv.get(key)

        async def kv_set(key, value):
            db.kv[key] = value

        db.kv_get = kv_get
        db.kv_set = kv_set
        with patch("app.services.weekly_report_service.Database") as db_cls, \
             patch("app.services.alert_service.alert_service") as mock_alert:
            db_cls.get.return_value = db
            mock_alert.broadcast_report = AsyncMock()
            result = await svc.generate()
        assert result["generated"] is True
        assert "自动化触发 2 次" in result["text"]
        assert "全部成功" in result["text"]

    def test_stats_summary(self):
        from app.services.weekly_report_service import WeeklyReportService
        text = WeeklyReportService._summarize_stats([
            {"kind": "alert"}, {"kind": "alert"}, {"kind": "alert_resolved"},
            {"kind": "task_failed"},
        ])
        assert "告警 2 次" in text
        assert "失败 1 次" in text
