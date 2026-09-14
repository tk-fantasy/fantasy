"""补充覆盖测试：services 层此前未覆盖的分支。

目标模块：
- app/services/alert_service.py — 通知/冷却/监控循环/摄像头与 HA 检查
- app/services/emoji_service.py — 索引加载/搜索/重建各失败分支
- app/services/rule_registry_service.py — DB 装配失败/触发时间/规则更新
- app/services/vision_service.py — 关注项 CRUD/条件评估/结果构建
- app/services/entity_controls.py — 控件推导的边角分支
- app/services/device_event_service.py — 订阅握手/事件分类节流/聚合落库
- app/services/control_probe.py — 探测失败兜底
- app/services/device_registry.py — 快照构建边界/渲染跳过分支
- app/services/semantic_map.py — 缓存加载失败/非对称 state
- app/services/health_check.py — 超时与异常降级

边界 mock：LLM/vision 客户端、HA websocket、Database（patch 类）、时间。
不触碰真实 app/data 与 logs/。
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import app.core.ws_registry as ws_registry
import app.services.alert_service as alert_mod
from app.services.alert_service import AlertService
from app.services.control_probe import (
    _is_out_of_range_error,
    _normalize_to_range,
    _probe_range,
    call_with_probe,
    _probe_cache,
)
from app.services.device_event_service import (
    DeviceEventService,
    record_device_op,
    _state_zh,
)
from app.services.device_registry import (
    build_device_snapshot,
    derive_sub_name,
    entry_label,
    render_catalog_text,
    render_controls_text,
    render_devices_brief,
    render_entities_flat,
)
from app.services.emoji_service import EmojiService
from app.services.health_check import HealthChecker
from app.services.rule_registry_service import RuleRegistryService
from app.services.semantic_map import (
    apply_state_flip,
    get_action_map,
    invalidate_cache,
)
from app.services.vision_service import VisionService, _feedback_default


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://ha/api/services/x/y")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def _mock_db(events=None):
    """patch app.core.database.Database 用 mock db（模式同 test_family_report_enhancements）。"""
    db = MagicMock()
    db.family_event_add = AsyncMock()
    db.family_events_since = AsyncMock(return_value=events or [])
    db.rules_all = AsyncMock(return_value=[])
    db.rules_insert = AsyncMock()
    db.rules_update = AsyncMock(return_value=True)
    db.rules_delete = AsyncMock(return_value=True)
    db.prefs_get_by_scope = AsyncMock(return_value={})
    return db


# ===========================================================================
# AlertService
# ===========================================================================

class TestAlertService:
    def _svc(self):
        return AlertService()

    async def test_notify_fires_once_then_cooldown(self):
        svc = self._svc()
        db = _mock_db()
        pushed = []
        async def fake_push(payload):
            pushed.append(payload)
        notifier = AsyncMock()
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", fake_push):
            db_cls.get.return_value = db
            svc.register_notifier("feishu", notifier)
            await svc.notify("camera:c1", "摄像头离线")
            await svc.notify("camera:c1", "摄像头又离线")  # 冷却期内，不重发
        assert notifier.await_count == 1
        notifier.assert_awaited_with("摄像头离线", "warning")
        assert len(pushed) == 1
        assert pushed[0]["type"] == "alert"
        assert svc._active["camera:c1"]["active"] is True
        db.family_event_add.assert_awaited_once()
        assert svc.notifiers == ["feishu"]

    async def test_notify_after_cooldown_expiry_refires(self):
        svc = self._svc()
        svc._active["camera:c9"] = {"alerted_at": time.time() - 30 * 60 - 1, "active": True}
        notifier = AsyncMock()
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock()):
            db_cls.get.return_value = db = _mock_db()
            svc.register_notifier("n", notifier)
            await svc.notify("camera:c9", "再次离线")
        assert notifier.await_count == 1

    async def test_notify_disabled_by_config(self, monkeypatch):
        monkeypatch.setattr(alert_mod, "get_config", lambda *a, **k: False)
        svc = self._svc()
        await svc.notify("camera:c1", "x")
        assert "camera:c1" not in svc._active

    async def test_is_enabled_exception_defaults_true(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("config broken")
        monkeypatch.setattr(alert_mod, "get_config", boom)
        assert self._svc()._is_enabled() is True

    async def test_notify_exception_swallowed(self):
        svc = self._svc()
        async def boom(*a, **k):
            raise RuntimeError("record failed")
        svc._record = boom
        await svc.notify("camera:c1", "x")  # 不应向外抛异常

    async def test_resolve_noop_when_not_active(self):
        svc = self._svc()
        notifier = AsyncMock()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = _mock_db()
            svc.register_notifier("n", notifier)
            await svc.resolve("camera:ghost", "恢复")
        notifier.assert_not_awaited()

    async def test_resolve_sends_restore_notice(self):
        svc = self._svc()
        svc._active["camera:c1"] = {"alerted_at": time.time(), "active": True}
        pushed = []
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock(side_effect=pushed.append)):
            db_cls.get.return_value = _mock_db()
            await svc.resolve("camera:c1", "已恢复在线")
        assert "camera:c1" not in svc._active
        assert pushed and "已恢复在线" in pushed[0]["message"]
        assert pushed[0]["level"] == "info"

    async def test_resolve_default_message_when_empty(self):
        svc = self._svc()
        svc._active["ha:connection"] = {"alerted_at": time.time(), "active": True}
        pushed = []
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock(side_effect=pushed.append)):
            db_cls.get.return_value = _mock_db()
            await svc.resolve("ha:connection")
        assert "ha:connection 已恢复" in pushed[0]["message"]

    async def test_resolve_exception_swallowed(self):
        svc = self._svc()
        svc._active["camera:c1"] = {"alerted_at": time.time(), "active": True}
        async def boom(*a, **k):
            raise RuntimeError("db down")
        svc._record = boom
        await svc.resolve("camera:c1", "x")  # 不应向外抛异常

    async def test_record_persists_with_actor(self):
        svc = self._svc()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = db = _mock_db()
            await svc.record("task_done", "scheduler:t1", "任务完成", actor="AI")
        db.family_event_add.assert_awaited_with("task_done", "scheduler:t1", "任务完成", "AI")

    async def test_record_exception_swallowed(self):
        svc = self._svc()
        async def boom(*a, **k):
            raise RuntimeError("x")
        svc._record = boom
        await svc.record("task_done", "s", "m")

    def test_register_overwrites_and_unregister(self):
        svc = self._svc()
        svc.register_notifier("a", AsyncMock())
        svc.register_notifier("a", AsyncMock())  # 重名覆盖
        svc.unregister_notifier("a")
        svc.unregister_notifier("ghost")  # 不存在也不报错
        assert svc.notifiers == []

    def test_bind_injects_dependencies(self):
        svc = self._svc()
        cm, hc = object(), object()
        svc.bind(camera_manager=cm, health_checker=hc)
        assert svc._camera_manager is cm
        assert svc._health_checker is hc

    async def test_start_stop_monitor_loop(self, monkeypatch, caplog):
        monkeypatch.setattr(alert_mod, "_MONITOR_INTERVAL_SECONDS", 0.01)
        svc = self._svc()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = _mock_db()
            await svc.start()
        assert svc._monitor_task is not None and not svc._monitor_task.done()
        await asyncio.sleep(0.05)
        await svc.stop()
        assert svc._monitor_task is None
        # 重复 stop 幂等
        await svc.stop()

    async def test_start_disabled_no_task(self, monkeypatch):
        monkeypatch.setattr(alert_mod, "get_config", lambda *a, **k: False)
        svc = self._svc()
        await svc.start()
        assert svc._monitor_task is None

    async def test_monitor_loop_survives_tick_exception(self, monkeypatch, caplog):
        monkeypatch.setattr(alert_mod, "_MONITOR_INTERVAL_SECONDS", 0.01)

        class BoomManager:
            def list_cameras(self):
                return [{"id": "c1", "name": "cam"}]
            def get_state(self, cid):
                raise RuntimeError("get_state exploded")

        svc = self._svc()
        svc.bind(camera_manager=BoomManager())
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = _mock_db()
            await svc.start()
            await asyncio.sleep(0.05)
            await svc.stop()
        assert "alert monitor tick failed" in caplog.text

    async def test_monitor_loop_reraises_cancel_during_check(self, monkeypatch):
        """stop() 的取消发生在 _check_ha await 中途 → CancelledError 原样上抛。"""
        monkeypatch.setattr(alert_mod, "_MONITOR_INTERVAL_SECONDS", 0.01)
        svc = self._svc()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_check_ha():
            entered.set()
            await release.wait()
        svc._check_ha = slow_check_ha
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = _mock_db()
            await svc.start()
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            stop_task = asyncio.create_task(svc.stop())
            await asyncio.sleep(0.02)  # 让 cancel 命中 _check_ha 的 await
            release.set()
            await asyncio.wait_for(stop_task, timeout=1.0)
        assert svc._monitor_task is None

    async def test_stop_cancels_spawned_notifier_tasks(self):
        svc = self._svc()
        async def idle():
            await asyncio.sleep(30)
        task = asyncio.create_task(idle())
        svc._monitor_tasks.add(task)
        await asyncio.sleep(0)  # 让任务先启动
        await svc.stop()        # 附带任务一并 cancel
        await asyncio.sleep(0)
        assert task.cancelled()

    async def test_broadcast_ws_push_failure_swallowed_on_alert(self):
        svc = self._svc()
        async def ws_boom(payload):
            raise RuntimeError("no ws client")
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", ws_boom):
            db_cls.get.return_value = _mock_db()
            await svc.notify("camera:c1", "离线")  # ws 推送失败只记 debug
            svc._active["camera:c2"] = {"alerted_at": time.time(), "active": True}
            await svc.resolve("camera:c2", "恢复")

    async def test_restore_exception_swallowed(self):
        svc = self._svc()
        db = _mock_db()
        db.family_events_since = AsyncMock(side_effect=RuntimeError("db locked"))
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = db
            await svc._restore_active_from_events()
        assert svc._active == {}

    # ---- _check_cameras ----

    class _CM:
        def __init__(self, cameras, states=None, list_raises=False):
            self._cameras = cameras
            self._states = states or {}
            self._list_raises = list_raises
        def list_cameras(self):
            if self._list_raises:
                raise RuntimeError("list failed")
            return self._cameras
        def get_state(self, cid):
            return self._states.get(cid, {})

    async def test_check_cameras_skips_when_no_manager(self):
        svc = self._svc()
        await svc._check_cameras()  # cm None → 静默返回

    async def test_check_cameras_survives_list_failure(self):
        svc = self._svc()
        svc.bind(camera_manager=self._CM([], list_raises=True))
        await svc._check_cameras()

    async def test_check_cameras_empty_list(self):
        svc = self._svc()
        svc.bind(camera_manager=self._CM([]))
        await svc._check_cameras()

    async def test_check_cameras_offline_two_ticks_then_alert_then_resolve(self):
        svc = self._svc()
        cm = self._CM([{"id": "c1", "name": "门口"}], states={})
        svc.bind(camera_manager=cm)
        pushed = []
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock(side_effect=pushed.append)):
            db_cls.get.return_value = _mock_db()
            await svc._check_cameras()  # 第 1 拍：仅计数，不打扰
            assert "camera:c1" not in svc._active
            await svc._check_cameras()  # 第 2 拍：告警
            assert "camera:c1" in svc._active
            assert any("门口" in p["message"] and p["type"] == "alert" for p in pushed)
            # 恢复在线 → resolve 推送
            cm._states["c1"] = {"camera_opened": True}
            await svc._check_cameras()
            assert "camera:c1" not in svc._active
            assert any("已恢复在线" in p["message"] for p in pushed)
        assert svc._camera_offline_ticks.get("c1") is None

    async def test_check_cameras_stale_tick_cleanup_and_empty_id(self):
        svc = self._svc()
        cm = self._CM([{"id": "", "name": "无 id"}, {"id": "c1", "name": "c1"}], states={})
        svc._camera_offline_ticks = {"deleted": 7, "c1": 1}
        svc.bind(camera_manager=cm)
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock()):
            db_cls.get.return_value = _mock_db()
            await svc._check_cameras()
        assert "deleted" not in svc._camera_offline_ticks  # 已删除摄像头计数被回收
        assert svc._camera_offline_ticks[""] == 1          # 空 id 也走离线计数

    # ---- _check_ha ----

    async def test_check_ha_skips_when_no_checker(self):
        await self._svc()._check_ha()

    async def test_check_ha_alerts_after_three_down_ticks(self):
        svc = self._svc()
        hc = MagicMock()
        hc.ha_available = False
        svc.bind(health_checker=hc)
        pushed = []
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock(side_effect=pushed.append)):
            db_cls.get.return_value = _mock_db()
            await svc._check_ha()
            await svc._check_ha()
            assert pushed == []  # 前 2 拍静默
            await svc._check_ha()
            assert any("Home Assistant 连接不可用" in p["message"] for p in pushed)
        hc.ha_available = True
        with patch("app.core.database.Database") as db_cls, \
             patch.object(ws_registry, "push_to_all", AsyncMock(side_effect=pushed.append)):
            db_cls.get.return_value = _mock_db()
            await svc._check_ha()
            assert svc._ha_down_ticks == 0
            assert any("已恢复" in p["message"] for p in pushed)

    # ---- broadcast ----

    async def test_broadcast_report_and_ws_failure_swallowed(self):
        svc = self._svc()
        notifier = AsyncMock()
        svc.register_notifier("n", notifier)
        async def ws_boom(payload):
            raise RuntimeError("no online users")
        with patch.object(ws_registry, "push_to_all", ws_boom):
            await svc.broadcast_report("每周家庭报告内容")
        notifier.assert_awaited_with("每周家庭报告内容", "info")

    async def test_dispatch_notifier_failure_does_not_block_others(self):
        svc = self._svc()
        bad = AsyncMock(side_effect=RuntimeError("channel down"))
        good = AsyncMock()
        svc.register_notifier("bad", bad)
        svc.register_notifier("good", good)
        with patch.object(ws_registry, "push_to_all", AsyncMock()):
            await svc._dispatch_notifiers("msg", "warning")
        good.assert_awaited_once_with("msg", "warning")


# ===========================================================================
# EmojiService
# ===========================================================================

def _embed_client(enabled=True, dim=2, responses=None):
    c = MagicMock()
    c.enabled = enabled
    c.model = "test-embed"
    c._role_cfg = MagicMock(return_value=1)
    if responses is not None:
        c.post_embedding = AsyncMock(side_effect=responses)
    else:
        c.post_embedding = AsyncMock(return_value={"embedding": [1.0] * dim})
    return c


class TestEmojiService:
    def test_properties_initial_state(self):
        svc = EmojiService(embed_client=_embed_client())
        assert svc.is_loaded is False
        assert svc.is_loading is False
        status = svc.rebuild_status
        assert status["running"] is False and status["done"] == 0

    def test_resolve_index_path_relative_and_absolute(self, monkeypatch):
        from pathlib import Path
        monkeypatch.setattr("app.services.emoji_service.get_config",
                            lambda k, d=None: "sub/dir/emoji_index.json")
        svc = EmojiService(embed_client=_embed_client())
        rel = svc._resolve_index_path()
        # 相对路径 → 相对项目根解析
        assert rel.is_absolute() and rel.parts[-3:] == ("sub", "dir", "emoji_index.json")
        monkeypatch.setattr("app.services.emoji_service.get_config",
                            lambda k, d=None: "C:/abs/emoji_index.json")
        assert svc._resolve_index_path() == Path("C:/abs/emoji_index.json")

    async def test_load_index_success(self, tmp_path):
        idx = tmp_path / "emoji_index.json"
        idx.write_text(json.dumps([
            {"char": "💡", "name": "light", "vec": [1.0, 0.0]},
            {"char": "🔥", "name": "fire", "vec": [0.0, 1.0]},
        ]), encoding="utf-8")
        svc = EmojiService(embed_client=_embed_client())
        with patch.object(svc, "_resolve_index_path", return_value=idx):
            await svc.load_index_async()
        assert svc.is_loaded is True
        assert svc._chars == ["💡", "🔥"]
        assert svc._norms.shape == (2, 1)

    async def test_load_index_missing_file_logs_and_stays_unloaded(self, tmp_path):
        svc = EmojiService(embed_client=_embed_client())
        with patch.object(svc, "_resolve_index_path",
                          return_value=tmp_path / "missing.json"):
            await svc.load_index_async()
        assert svc.is_loaded is False

    async def test_load_index_skips_when_already_loaded_or_loading(self, tmp_path):
        idx = tmp_path / "emoji_index.json"
        idx.write_text(json.dumps([{"char": "💡", "name": "l", "vec": [1.0, 0.0]}]),
                       encoding="utf-8")
        svc = EmojiService(embed_client=_embed_client())
        with patch.object(svc, "_resolve_index_path", return_value=idx):
            await svc.load_index_async()
            await svc.load_index_async()  # _loaded → 直接返回
        assert svc._chars == ["💡"]
        svc._loading = True
        await svc.load_index_async()  # _loading → 直接返回
        assert svc._chars == ["💡"]

    async def test_search_ranks_by_cosine_similarity(self, tmp_path):
        idx = tmp_path / "emoji_index.json"
        idx.write_text(json.dumps([
            {"char": "💡", "name": "light", "vec": [1.0, 0.0]},
            {"char": "🔥", "name": "fire", "vec": [0.0, 1.0]},
        ]), encoding="utf-8")
        client = _embed_client(responses=[{"embedding": [0.9, 0.1]}])
        svc = EmojiService(embed_client=client)
        with patch.object(svc, "_resolve_index_path", return_value=idx):
            await svc.load_index_async()
        results = await svc.search("light", top_k=1)
        assert len(results) == 1
        assert results[0]["char"] == "💡"
        assert results[0]["score"] >= 0.9

    async def test_search_empty_when_not_loaded(self):
        svc = EmojiService(embed_client=_embed_client())
        assert await svc.search("light") == []

    async def test_search_embed_failure_returns_empty(self, tmp_path):
        idx = tmp_path / "emoji_index.json"
        idx.write_text(json.dumps([{"char": "💡", "name": "l", "vec": [1.0, 0.0]}]),
                       encoding="utf-8")
        client = _embed_client(responses=[RuntimeError("embed down")])
        svc = EmojiService(embed_client=client)
        with patch.object(svc, "_resolve_index_path", return_value=idx):
            await svc.load_index_async()
        assert await svc.search("light") == []

    async def test_search_zero_norm_query_returns_empty(self, tmp_path):
        idx = tmp_path / "emoji_index.json"
        idx.write_text(json.dumps([{"char": "💡", "name": "l", "vec": [1.0, 0.0]}]),
                       encoding="utf-8")
        client = _embed_client(responses=[{"embedding": [0.0, 0.0]}])
        svc = EmojiService(embed_client=client)
        with patch.object(svc, "_resolve_index_path", return_value=idx):
            await svc.load_index_async()
        assert await svc.search("l") == []

    async def test_rebuild_skips_when_already_running(self):
        svc = EmojiService(embed_client=_embed_client())
        svc._rebuild_running = True
        await svc.rebuild_index()
        assert svc.rebuild_status["running"] is True

    async def test_rebuild_aborts_when_embed_disabled(self):
        svc = EmojiService(embed_client=_embed_client(enabled=False))
        await svc.rebuild_index()
        assert "未配置或未启用" in svc.rebuild_status["message"]
        assert svc.rebuild_status["running"] is False

    async def test_rebuild_aborts_when_index_and_seed_missing(self, tmp_path):
        svc = EmojiService(embed_client=_embed_client())
        with patch.object(svc, "_resolve_index_path",
                          return_value=tmp_path / "no_index.json"), \
             patch("app.services.emoji_service.SEED_PATH",
                   tmp_path / "no_seed.json"):
            await svc.rebuild_index()
        assert "索引与种子均不存在" in svc.rebuild_status["message"]

    async def test_rebuild_aborts_when_seed_unreadable(self, tmp_path):
        seed_dir = tmp_path / "seed_dir"
        seed_dir.mkdir()  # 目录无法当 JSON 打开
        svc = EmojiService(embed_client=_embed_client())
        with patch.object(svc, "_resolve_index_path",
                          return_value=tmp_path / "no_index.json"), \
             patch("app.services.emoji_service.SEED_PATH", seed_dir):
            await svc.rebuild_index()
        assert "种子数据读取失败" in svc.rebuild_status["message"]

    async def test_rebuild_success_with_embed_failure_fallback(self, tmp_path):
        idx = tmp_path / "emoji_index.json"
        seed = tmp_path / "emoji_seed.json"
        seed.write_text(json.dumps([
            {"char": "💡", "code": "U+1F4A1", "name": "light"},
            {"char": "🔥", "code": "U+1F525", "name": "fire", "vec": [0.5, 0.5]},
        ]), encoding="utf-8")
        client = _embed_client(responses=[
            {"embedding": [1.0, 0.0]},   # 第 1 条成功
            RuntimeError("embed quota"),  # 第 2 条失败 → 保留旧向量
        ])
        svc = EmojiService(embed_client=client)
        with patch.object(svc, "_resolve_index_path", return_value=idx), \
             patch("app.services.emoji_service.SEED_PATH", seed), \
             patch.object(svc, "load_index_async", new_callable=AsyncMock):
            await svc.rebuild_index()
        status = svc.rebuild_status
        assert status["running"] is False
        assert status["errors"] == 1 and status["done"] == 2
        assert "重建完成" in status["message"]
        written = json.loads(idx.read_text(encoding="utf-8"))
        by_char = {w["char"]: w for w in written}
        assert by_char["💡"]["vec"] == [1.0, 0.0]
        assert by_char["🔥"]["vec"] == [0.5, 0.5]  # 旧向量兜底，不丢条目

    async def test_rebuild_generic_failure_reports_message(self, tmp_path):
        seed = tmp_path / "emoji_seed.json"
        seed.write_text(json.dumps([{"char": "💡", "name": "light"}]), encoding="utf-8")
        svc = EmojiService(embed_client=_embed_client())
        with patch.object(svc, "_resolve_index_path",
                          return_value=tmp_path / "no_index.json"), \
             patch("app.services.emoji_service.SEED_PATH", seed), \
             patch.object(EmojiService, "_write_file", side_effect=OSError("disk full")):
            await svc.rebuild_index()
        assert svc.rebuild_status["message"] == "重建失败，请查看后端日志"
        assert svc.rebuild_status["running"] is False


# ===========================================================================
# RuleRegistryService
# ===========================================================================

class TestRuleRegistryService:
    async def test_load_from_db_populates_and_is_idempotent(self):
        db = _mock_db()
        db.rules_all = AsyncMock(return_value=[
            {"id": "r1", "trigger": {"type": "time"}, "condition": "下雨",
             "actions": [], "summary": "s", "enabled": True,
             "created_at": 1000, "updated_at": 1000, "name": "规则1"},
            {"id": "r2", "condition": "看到猫", "type": ""},
        ])
        svc = RuleRegistryService()
        with patch("app.services.rule_registry_service.Database") as db_cls:
            db_cls.get.return_value = db
            await svc.load_from_db()
            await svc.load_from_db()  # _loaded → 第二次直接返回，不重复加载
        assert len(svc.list_rules()) == 2
        by_id = {r["id"]: r for r in svc.list_rules()}
        assert by_id["r1"]["type"] == "weather"   # 按 condition 关键词猜类型
        assert by_id["r2"]["type"] == "vision"    # 显式空串 → 猜测兜底

    async def test_load_from_db_failure_starts_fresh(self):
        svc = RuleRegistryService()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.side_effect = RuntimeError("Database not initialized")
            await svc.load_from_db()
        assert svc.list_rules() == []
        assert svc._loaded is True

    def test_spawn_task_without_loop_is_swallowed(self):
        svc = RuleRegistryService()
        async def coro():
            pass
        c = coro()
        svc._spawn_task(c)  # 无事件循环 → RuntimeError 被吞
        c.close()

    async def test_save_insert_delete_async_tolerate_missing_db(self, monkeypatch):
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod.Database, "_instance", None)
        svc = RuleRegistryService()
        rule = svc.add_rule({"id": "r1", "condition": "x"})
        svc._save_rule_async(svc._rules[0])
        svc._insert_rule_async(svc._rules[0])
        svc._delete_rule_async("r1")
        await asyncio.sleep(0)  # 让可能的任务回调跑完
        assert rule["id"] == "r1"

    async def test_log_task_error_logs_exception(self, caplog):
        async def boom():
            raise ValueError("task failed")
        task = asyncio.create_task(boom())
        with pytest.raises(ValueError):
            await task
        RuleRegistryService._log_task_error(task)
        assert "Background DB task failed" in caplog.text

    async def test_update_trigger_time_persists(self):
        db = _mock_db()
        svc = RuleRegistryService()
        with patch("app.services.rule_registry_service.Database") as db_cls:
            db_cls.get.return_value = db
            svc.add_rule({"id": "r1", "condition": "x"})
            svc.update_trigger_time("r1", 123.5)
            svc.update_trigger_time("ghost", 1.0)  # 不存在的 id → 静默
            await asyncio.sleep(0.05)
        assert svc.get_rule("r1")["last_triggered_at"] == 123.5
        db.rules_update.assert_awaited()

    async def test_update_rule_overrides_schema_fields_only(self):
        db = _mock_db()
        svc = RuleRegistryService()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = db
            svc.add_rule({"id": "r1", "condition": "old", "name": "旧名",
                          "summary": "旧摘要", "cooldown_seconds": 9,
                          "user_id": "u1"})
            updated = svc.update_rule("r1", {
                "name": "新名", "condition": "新条件", "type": "time",
                "actions": [{"service": "turn_on"}],
                "action_descriptions": ["开灯"], "cooldown_seconds": 30,
                "summary": "新摘要",
            })
        assert updated["name"] == "新名"
        assert updated["condition"] == "新条件"
        assert updated["type"] == "time"
        assert updated["cooldown_seconds"] == 30
        assert updated["user_id"] == "u1"          # 保留不被覆盖
        assert updated["enabled"] is True
        assert updated["last_triggered_at"] == 0.0

    async def test_update_rule_nonexistent_raises_404(self):
        from app.core.exceptions import AppException
        svc = RuleRegistryService()
        svc.add_rule({"id": "r1", "condition": "x"})
        with pytest.raises(AppException) as exc:
            svc.update_rule("ghost", {"name": "n"})
        assert exc.value.http_status == 404

    async def test_get_rule_found_and_missing(self):
        svc = RuleRegistryService()
        svc.add_rule({"id": "r1", "condition": "x", "name": "规则"})
        assert svc.get_rule("r1")["name"] == "规则"
        assert svc.get_rule("ghost") is None

    async def test_set_enabled_and_delete_nonexistent_raise_404(self):
        from app.core.exceptions import AppException
        svc = RuleRegistryService()
        with patch("app.core.database.Database") as db_cls:
            db_cls.get.return_value = _mock_db()
            with pytest.raises(AppException):
                svc.set_enabled("ghost", True)
            with pytest.raises(AppException):
                svc.delete_rule("ghost")


# ===========================================================================
# VisionService
# ===========================================================================

def _vision_client(enabled=True, model="vl-model"):
    c = MagicMock()
    c.enabled = enabled
    c.model = model
    c.set_key_pool = MagicMock()
    c._max_side = 448
    c._jpeg_quality = 70
    return c


class TestVisionService:
    def test_feedback_default_known_and_unknown_events(self):
        assert _feedback_default("person_waving") == "识别到挥手动作。"
        assert _feedback_default("custom_event") == "检测到事件: custom_event"

    def test_set_key_pool_forwards_to_client(self):
        client = _vision_client()
        svc = VisionService(client)
        svc.set_key_pool("POOL")
        client.set_key_pool.assert_called_once_with("POOL")

    def test_model_and_enabled_properties(self):
        svc = VisionService(_vision_client(enabled=False, model="m1"))
        assert svc.model == "m1"
        assert svc.enabled is False

    async def test_encode_frames_b64_uses_thread_helper(self, monkeypatch):
        seen = {}
        def fake_encode(frames, max_side, quality):
            seen["args"] = (frames, max_side, quality)
            return ["b64a", "b64b"]
        monkeypatch.setattr("app.clients.llm_vision_client._encode_frames_b64",
                            fake_encode)
        svc = VisionService(_vision_client())
        out = await svc.encode_frames_b64([object(), object()])
        assert out == ["b64a", "b64b"]
        assert seen["args"][1:] == (448, 70)

    def test_focus_crud(self):
        svc = VisionService(_vision_client())
        item = svc.add_focus("有人出现", camera_id="cam1")
        assert svc.get_vision_focuses("cam1") == [item]
        assert svc.get_vision_focuses("other") == []
        assert svc.get_all_focuses_flat() == [item]
        updated = svc.update_focus(item["id"], text="有人离开", enabled=False,
                                   camera_id="cam1")
        assert updated["text"] == "有人离开" and updated["enabled"] is False
        assert svc.update_focus("ghost", camera_id="cam1") is None
        assert svc.delete_focus(item["id"], camera_id="cam1") is True
        assert svc.delete_focus(item["id"], camera_id="cam1") is False

    def test_load_focuses_rebuckets_by_camera(self):
        svc = VisionService(_vision_client())
        svc.load_focuses([
            {"id": "a", "text": "t1", "enabled": True, "camera_id": "cam1"},
            {"id": "b", "text": "t2", "enabled": True, "camera_id": ""},
        ])
        assert [f["id"] for f in svc.get_vision_focuses("cam1")] == ["a"]
        assert [f["id"] for f in svc.get_vision_focuses("")] == ["b"]

    def test_combined_focus_only_enabled(self):
        svc = VisionService(_vision_client())
        a = svc.add_focus("猫", camera_id="cam1")
        b = svc.add_focus("狗", camera_id="cam1")
        svc.update_focus(b["id"], enabled=False, camera_id="cam1")
        assert svc._get_combined_focus("cam1") == "猫"

    async def test_evaluate_condition_parses_first_digit(self):
        cases = [("1", 1), ("0", 0), ("答案是：2 满足", 1), ("10 是", 1)]
        for raw, expected in cases:
            client = _vision_client()
            client.evaluate_condition = AsyncMock(return_value=raw)
            svc = VisionService(client)
            assert await svc.evaluate_condition([1], "有人吗") == expected, raw

    async def test_evaluate_condition_no_digits_returns_zero(self, caplog):
        client = _vision_client()
        client.evaluate_condition = AsyncMock(return_value="无法判断")
        svc = VisionService(client)
        assert await svc.evaluate_condition([1], "有人吗") == 0

    def test_classify_frame_sync_disabled_returns_idle(self):
        svc = VisionService(_vision_client(enabled=False))
        res = svc.classify_frame(b"frame")
        assert res.action == "idle"
        assert res.details["enabled"] is False

    def test_classify_frame_sync_builds_result(self):
        payload = {"choices": [{"message": {"content":
            json.dumps({"event": "person_waving", "observation": "有人在挥手"})}}]}
        client = _vision_client()
        async def fake_classify(frame, focus=None):
            return payload
        client.classify_frame = fake_classify
        svc = VisionService(client)
        svc.add_focus("挥手", camera_id="cam1")
        res = svc.classify_frame(b"frame", camera_id="cam1")
        assert res.action == "person_waving"
        assert res.feedback == "有人在挥手"
        assert res.details["model"] == "vl-model"

    async def test_classify_frame_async_disabled_returns_idle(self):
        svc = VisionService(_vision_client(enabled=False))
        res = await svc.classify_frame_async(b"frame")
        assert res.action == "idle"
        assert res.details["enabled"] is False

    async def test_classify_frame_async_returns_result(self):
        payload = {"choices": [{"message": {"content":
            json.dumps({"event": "pet_detected", "observation": ""})}}]}
        client = _vision_client()
        async def fake_classify(frame, focus=None):
            return payload
        client.classify_frame = fake_classify
        svc = VisionService(client)
        res = await svc.classify_frame_async(b"frame")
        assert res.action == "pet_detected"
        assert res.feedback == _feedback_default("pet_detected")

    def test_build_result_no_event_uses_default_feedback(self):
        svc = VisionService(_vision_client())
        payload = {"choices": [{"message": {"content":
            json.dumps({"event": "no_event", "observation": ""})}}]}
        res = svc._build_result_from_payload(payload)
        assert res.action == "no_event"
        assert res.feedback == "画面平静，暂无事件。"

    def test_build_result_empty_choices_yields_parse_error_default(self):
        svc = VisionService(_vision_client())
        res = svc._build_result_from_payload({"choices": []})
        assert res.action == "no_event"
        assert res.details["parse_error"] is True

    def test_parse_json_invalid_raises_vision_exception(self):
        from app.core.exceptions import VisionInferenceException
        svc = VisionService(_vision_client())
        with pytest.raises(VisionInferenceException):
            svc._parse_json("完全不是 JSON")


# ===========================================================================
# DeviceEventService
# ===========================================================================

class FakeWS:
    """可编程 fake websocket：recv 依次弹出 incoming，耗尽后抛 CancelledError。"""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def recv(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise asyncio.CancelledError()

    async def send(self, text):
        self.sent.append(text)


def _handshake_msgs(ack_success=True):
    return [
        json.dumps({"type": "auth_required"}),
        json.dumps({"type": "auth_ok"}),
        json.dumps({"id": 7, "type": "event"}),  # 订阅 ack 前推送的缓存事件，应被跳过
        json.dumps({"id": 1, "success": ack_success,
                    **({} if ack_success else {"error": {"code": "x"}})}),
    ]


@pytest.fixture
def no_alert_record(monkeypatch):
    """把全局 alert_service.record 换成 AsyncMock，隔离 DB。"""
    import app.services.alert_service as als
    mock = AsyncMock()
    monkeypatch.setattr(als.alert_service, "record", mock)
    return mock


class TestDeviceEventService:
    async def test_record_device_op_empty_entity_ids(self, no_alert_record):
        await record_device_op([], "turn_on")
        no_alert_record.assert_not_awaited()

    async def test_record_device_op_failure_swallowed(self, monkeypatch):
        import app.services.alert_service as als
        async def boom(*a, **k):
            raise RuntimeError("record exploded")
        monkeypatch.setattr(als.alert_service, "record", boom)
        await record_device_op(["light.x"], "turn_on")  # 失败只记日志

    async def test_record_device_op_uses_friendly_name_and_zh_label(self, no_alert_record):
        await record_device_op(["light.hall"], "turn_on", actor="手动",
                               name_of={"light.hall": "会客厅灯"})
        no_alert_record.assert_awaited_once_with(
            "device_op", "device:light.hall", "手动将「会客厅灯」执行 打开", "手动")

    def test_set_ha_service(self):
        svc = DeviceEventService()
        ha = object()
        svc.set_ha_service(ha)
        assert svc._ha_service is ha

    async def test_start_disabled_creates_no_tasks(self, monkeypatch):
        import app.services.device_event_service as dev_mod
        monkeypatch.setattr(dev_mod, "get_config", lambda *a, **k: False)
        svc = DeviceEventService()
        await svc.start()
        assert svc._task is None and svc._flush_task is None

    async def test_start_and_stop_lifecycle(self, monkeypatch):
        real_sleep = asyncio.sleep
        async def fake_sleep(t, *a, **k):
            if t >= 5:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        svc = DeviceEventService()  # 无 ha_service → _run 慢轮询路径
        await svc.start()
        assert svc._task is not None and svc._flush_task is not None
        await asyncio.sleep(0.05)  # 等 _run/_flush_loop 进入 sleep 即被取消
        monkeypatch.setattr(svc, "_flush_sensors",
                            AsyncMock(side_effect=RuntimeError("final flush boom")))
        await svc.stop()  # 停机冲刷失败静默（127-128）
        assert svc._task is None and svc._flush_task is None

    async def test_run_unconfigured_retries_until_cancelled(self, monkeypatch):
        """HA 未配置 → sleep(60) 后 continue 慢轮询，直到外层取消。"""
        real_sleep = asyncio.sleep
        calls = {"n": 0}
        async def fake_sleep(t, *a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        ha = MagicMock()
        ha._client = MagicMock(base_url="", token="")  # 配置为空
        svc = DeviceEventService(ha)
        with pytest.raises(asyncio.CancelledError):
            await svc._run()
        assert calls["n"] == 2

    async def test_run_full_stream_with_fake_ws(self, monkeypatch, no_alert_record):
        """connect 成功 → 握手订阅 → 消费事件 → recv 取消退出。"""
        class Client:
            base_url = "http://ha:8123"
            token = "tok"
        ha = MagicMock()
        ha._client = Client()

        class FakeConnect:
            def __init__(self, url, additional_headers=None):
                assert url.endswith("/api/websocket")
                assert additional_headers["Authorization"] == "Bearer tok"
            async def __aenter__(self):
                return FakeWS(_handshake_msgs() + [json.dumps({
                    "type": "event",
                    "event": {"event_type": "state_changed",
                              "data": {"entity_id": "light.hall",
                                       "old_state": {"state": "off"},
                                       "new_state": {"state": "on",
                                                     "attributes": {"friendly_name": "会客厅灯"}}}}}),
                ])
            async def __aexit__(self, *exc):
                return False
        monkeypatch.setattr("websockets.connect", FakeConnect)

        svc = DeviceEventService(ha)
        # 队列耗尽后 FakeWS.recv 抛 CancelledError → _run 原样上抛
        with pytest.raises(asyncio.CancelledError):
            await svc._run()
        no_alert_record.assert_awaited_with(
            "device_state", "device:light.hall", "会客厅灯 开")

    async def test_run_reconnects_with_backoff_then_cancels(self, monkeypatch):
        real_sleep = asyncio.sleep
        sleeps = []
        async def fake_sleep(t, *a, **k):
            sleeps.append(t)
            if t >= 10:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        class Client:
            base_url = "http://ha:8123"
            token = "tok"
        ha = MagicMock()
        ha._client = Client()

        def connect_boom(*a, **k):
            raise RuntimeError("ws refused")
        monkeypatch.setattr("websockets.connect", connect_boom)

        svc = DeviceEventService(ha)
        with pytest.raises(asyncio.CancelledError):
            await svc._run()
        # 第一次退避 5s（返回）→ backoff 翻倍 10s（触发取消）
        assert sleeps == [5.0, 10.0]

    async def test_run_sleeps_when_ha_unconfigured(self, monkeypatch):
        real_sleep = asyncio.sleep
        async def fake_sleep(t, *a, **k):
            raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        svc = DeviceEventService(ha_service=None)  # 无 client → base_url 为空
        with pytest.raises(asyncio.CancelledError):
            await svc._run()

    async def test_subscribe_full_handshake(self):
        ws = FakeWS(_handshake_msgs())
        ha = MagicMock()
        ha._client = MagicMock(token="tok123")
        svc = DeviceEventService(ha)
        await svc._subscribe(ws)
        assert json.loads(ws.sent[0]) == {"type": "auth", "access_token": "tok123"}
        assert json.loads(ws.sent[1])["type"] == "subscribe_events"

    async def test_subscribe_unexpected_hello_raises(self):
        ws = FakeWS([json.dumps({"type": "something_else"})])
        svc = DeviceEventService(MagicMock())
        with pytest.raises(RuntimeError, match="unexpected HA handshake"):
            await svc._subscribe(ws)

    async def test_subscribe_auth_failure_raises(self):
        ws = FakeWS([
            json.dumps({"type": "auth_required"}),
            json.dumps({"type": "auth_invalid"}),
        ])
        ha = MagicMock()
        ha._client = MagicMock(token="bad")
        svc = DeviceEventService(ha)
        with pytest.raises(RuntimeError, match="auth failed"):
            await svc._subscribe(ws)

    async def test_subscribe_ack_failure_raises(self):
        ws = FakeWS(_handshake_msgs(ack_success=False))
        ha = MagicMock()
        ha._client = MagicMock(token="tok")
        svc = DeviceEventService(ha)
        with pytest.raises(RuntimeError, match="subscribe_events failed"):
            await svc._subscribe(ws)

    async def test_consume_dispatches_state_changed_events(self, no_alert_record, monkeypatch):
        handled = []
        async def spy(entity_id, old, new):
            handled.append((entity_id, old, new))
        svc = DeviceEventService()
        monkeypatch.setattr(svc, "_on_state_changed", spy)
        ws = FakeWS([
            json.dumps({"type": "result"}),  # 非 event → 跳过
            json.dumps({"type": "event", "event": {"event_type": "other"}}),  # 非目标事件
            json.dumps({"type": "event", "event": {"event_type": "state_changed",
                "data": {"entity_id": "light.hall",
                         "old_state": {"state": "off"},
                         "new_state": {"state": "on"}}}}),
        ])
        with pytest.raises(asyncio.CancelledError):
            await svc._consume(ws)
        assert handled == [("light.hall", {"state": "off"}, {"state": "on"})]

    async def test_consume_swallows_handler_exception(self, monkeypatch):
        svc = DeviceEventService()
        async def boom(entity_id, old, new):
            raise RuntimeError("handler exploded")
        monkeypatch.setattr(svc, "_on_state_changed", boom)
        ws = FakeWS([
            json.dumps({"type": "event", "event": {"event_type": "state_changed",
                "data": {"entity_id": "light.hall",
                         "old_state": {"state": "off"},
                         "new_state": {"state": "on"}}}}),
        ])
        with pytest.raises(asyncio.CancelledError):
            await svc._consume(ws)  # 异常被捕获记日志，继续循环直到队列耗尽

    async def test_on_state_changed_filters_and_classifies(self, no_alert_record):
        svc = DeviceEventService()
        # 空实体 / 实体删除 → 忽略
        await svc._on_state_changed("", {"state": "off"}, {"state": "on"})
        await svc._on_state_changed("light.x", {"state": "off"}, None)
        no_alert_record.assert_not_awaited()
        # 仅属性变化 → 忽略
        await svc._on_state_changed("light.x", {"state": "on"}, {"state": "on"})
        no_alert_record.assert_not_awaited()
        # old_state None（首见）按不变处理 → 忽略
        await svc._on_state_changed("light.x", None, {"state": "on"})
        no_alert_record.assert_not_awaited()
        # 变不可用 → 即时记录
        await svc._on_state_changed(
            "light.x", {"state": "on"},
            {"state": "unavailable", "attributes": {"friendly_name": "客厅灯"}})
        no_alert_record.assert_awaited_with(
            "device_state", "device:light.x", "客厅灯 变为不可用")
        # 控制类 domain → 翻译后即时记录
        await svc._on_state_changed(
            "lock.door", {"state": "unlocked"},
            {"state": "locked", "attributes": {"friendly_name": "大门"}})
        no_alert_record.assert_awaited_with(
            "device_state", "device:lock.door", "大门 已上锁")
        # sensor domain → 进缓冲，不直接落库
        await svc._on_state_changed(
            "sensor.temp", {"state": "20"},
            {"state": "25", "attributes": {"unit_of_measurement": "°C"}})
        assert "sensor.temp" in svc._sensor_buffer
        buf = svc._sensor_buffer["sensor.temp"]
        assert buf["count"] == 1 and buf["min"] == 25 and buf["max"] == 25
        assert buf["unit"] == "°C"
        # 无 friendly_name → 回退 entity_id
        await svc._on_state_changed(
            "sensor.hum", {"state": "40"}, {"state": "50", "attributes": {}})
        assert svc._sensor_buffer["sensor.hum"]["name"] == "sensor.hum"

    def test_buffer_sensor_numeric_and_non_numeric_mix(self):
        svc = DeviceEventService()
        svc._buffer_sensor("sensor.a", "温度", "abc",
                           {"unit_of_measurement": "°C"})  # 非数值首条
        assert svc._sensor_buffer["sensor.a"]["min"] is None
        assert svc._sensor_buffer["sensor.a"]["unit"] == "°C"
        svc._buffer_sensor("sensor.a", "温度", "3", {"unit_of_measurement": "°C"})
        buf = svc._sensor_buffer["sensor.a"]
        assert buf["min"] == 3 and buf["max"] == 3  # None 基线 → 直接赋值
        assert buf["count"] == 2
        svc._buffer_sensor("sensor.a", "温度", "1", {})
        svc._buffer_sensor("sensor.a", "sensor.a", "5", {})  # 无友好名不刷新
        buf = svc._sensor_buffer["sensor.a"]
        assert (buf["min"], buf["max"]) == (1, 5)
        assert buf["name"] == "温度" and buf["unit"] == "°C"

    async def test_flush_sensors_window_and_force(self, no_alert_record):
        svc = DeviceEventService()
        svc._sensor_buffer = {
            "sensor.future": {"name": "未来", "count": 2, "min": 1, "max": 2,
                              "last": "2", "unit": "°C",
                              "flush_at": time.time() + 3600},
            "sensor.due": {"name": "到期", "count": 3, "min": 20, "max": 26,
                           "last": "26", "unit": "°C", "flush_at": time.time() - 1},
            "sensor.flat": {"name": "平直", "count": 2, "min": 5, "max": 5,
                            "last": "5", "unit": "", "flush_at": time.time() - 1},
            "sensor.nums": {"name": "非数", "count": 2, "min": None, "max": None,
                            "last": "x", "unit": "", "flush_at": time.time() - 1},
        }
        await svc._flush_sensors()  # 未到窗口的不动
        assert "sensor.future" in svc._sensor_buffer
        await svc._flush_sensors(force=True)  # force 全部落库
        assert svc._sensor_buffer == {}
        msgs = {call.args[1]: call.args[2] for call in no_alert_record.await_args_list}
        assert msgs["device:sensor.due"] == "到期 1 小时内变化 3 次（20~26°C）"
        assert msgs["device:sensor.flat"] == "平直 1 小时内变化 2 次"
        assert "（" not in msgs["device:sensor.nums"]

    async def test_flush_loop_survives_flush_failure(self, monkeypatch):
        real_sleep = asyncio.sleep
        calls = {"n": 0}
        async def fake_sleep(t, *a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        svc = DeviceEventService()
        monkeypatch.setattr(svc, "_flush_sensors",
                            AsyncMock(side_effect=ValueError("db gone")))
        with pytest.raises(asyncio.CancelledError):
            await svc._flush_loop()
        svc._flush_sensors.assert_awaited_once()

    async def test_flush_loop_reraises_cancel_during_flush(self, monkeypatch):
        real_sleep = asyncio.sleep
        calls = {"n": 0}
        async def fake_sleep(t, *a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError()
            await real_sleep(0)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        svc = DeviceEventService()
        monkeypatch.setattr(svc, "_flush_sensors",
                            AsyncMock(side_effect=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await svc._flush_loop()  # flush 中的取消不吞，原样上抛

    def test_is_enabled_exception_defaults_true(self, monkeypatch):
        import app.services.device_event_service as dev_mod
        def boom(*a, **k):
            raise RuntimeError("cfg")
        monkeypatch.setattr(dev_mod, "get_config", boom)
        assert DeviceEventService._is_enabled() is True

    def test_flush_seconds_exception_defaults_hour(self, monkeypatch):
        import app.services.device_event_service as dev_mod
        def boom(*a, **k):
            raise RuntimeError("cfg")
        monkeypatch.setattr(dev_mod, "get_config", boom)
        assert DeviceEventService._flush_seconds() == 3600.0

    def test_state_zh_translation(self):
        assert _state_zh("on") == "开"
        assert _state_zh("weird_state") == "weird_state"


# ===========================================================================
# ControlProbe
# ===========================================================================

class ProbeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append(data)
        if not self.responses:
            return {"ok": True}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestControlProbe:
    def test_out_of_range_error_requires_http_400(self):
        assert _is_out_of_range_error(ValueError("x")) is False
        assert _is_out_of_range_error(_http_error(500)) is False
        assert _is_out_of_range_error(_http_error(400)) is True

    async def test_call_with_probe_non_numeric_passthrough(self):
        client = ProbeClient([{"ok": 1}])
        res = await call_with_probe(client, "switch", "turn_on", "s1", None)
        assert res == {"ok": 1}
        res = await call_with_probe(client, "light", "turn_on", "s1",
                                    {"brightness": 50, "effect": "x"})  # 多参
        assert client.calls[-1] == {"brightness": 50, "effect": "x"}
        res = await call_with_probe(client, "switch", "turn_on", "s1",
                                    {"on": True})  # bool 不是滑块
        assert client.calls[-1] == {"on": True}

    async def test_call_with_probe_reraises_non_400(self):
        client = ProbeClient([_http_error(500)])
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_probe(client, "media_player", "volume_set", "s1",
                                  {"volume_level": 50})
        assert len(client.calls) == 1

    async def test_call_with_probe_reraises_generic_error(self):
        client = ProbeClient([ValueError("timeout")])
        with pytest.raises(ValueError):
            await call_with_probe(client, "media_player", "volume_set", "s1",
                                  {"volume_level": 50})

    def test_normalize_to_range_value_below_min_passthrough(self):
        assert _normalize_to_range(-5, (0, 100)) == -5

    async def test_probe_range_gives_up_on_500(self):
        client = ProbeClient([_http_error(500)])
        assert await _probe_range(client, "d", "svc", "e1", "p") is None

    async def test_probe_range_gives_up_on_generic_error(self):
        client = ProbeClient([RuntimeError("conn reset")])
        assert await _probe_range(client, "d", "svc", "e1", "p") is None

    async def test_probe_range_succeeds_on_second_scale(self):
        # 0-1 刻度 400 → 0-100 刻度中点 50 成功
        client = ProbeClient([_http_error(400), {"ok": 1}])
        rng = await _probe_range(client, "d", "svc", "e1", "p")
        assert rng == (0.0, 100.0)

    async def test_call_with_probe_cache_hit_normalizes(self):
        _probe_cache.set("e1", "volume_level", (0.0, 1.0))
        client = ProbeClient([])
        await call_with_probe(client, "media_player", "volume_set", "e1",
                              {"volume_level": 50})
        # 50 按 0-1 刻度归一化 → 0.5
        assert client.calls[-1] == {"volume_level": 0.5}
        _probe_cache.clear()


# ===========================================================================
# entity_controls 边角分支
# ===========================================================================

from app.services.entity_controls import (  # noqa: E402
    _concept_match,
    controls_to_text,
    resolve_controls,
)


class TestEntityControlsEdges:
    def test_enum_from_available_modes_list(self):
        services = {"fan": {"set_mode": {"fields": ["mode"]}}}
        entity = {"entity_id": "fan.x", "state": "auto",
                  "attributes": {"available_modes": ["auto", "low"]}}
        controls = resolve_controls(entity, services)
        assert controls["mode"]["type"] == "enum"
        assert controls["mode"]["options"] == ["auto", "low"]
        assert controls["mode"]["current"] == "auto"  # 回退 state

    def test_current_attr_skipped_when_base_attr_exists(self):
        services = {"climate": {"set_temperature": {"fields": ["temperature"]}}}
        entity = {"entity_id": "climate.ac", "state": "cool",
                  "attributes": {"temperature": 22, "current_temperature": 24}}
        controls = resolve_controls(entity, services)
        assert "current_temperature" not in controls  # 是传感器读数，跳过
        assert controls["temperature"]["current"] == 22

    def test_current_attr_without_any_field_match_is_skipped(self):
        services = {"fan": {"turn_on": {"fields": ["entity_id"]}}}
        entity = {"entity_id": "fan.x", "state": "on",
                  "attributes": {"current_foo": 3}}
        controls = resolve_controls(entity, services)
        assert "current_foo" not in controls  # 无字段可匹配 → 不生成滑块

    def test_raw_brightness_skipped_when_only_non_pct_field(self):
        services = {"light": {"turn_on": {"fields": ["brightness"]}}}
        entity = {"entity_id": "light.x", "state": "on",
                  "attributes": {"brightness": 128}}
        assert resolve_controls(entity, services) == {}  # 原始 brightness 不生成滑块

    def test_action_does_not_overwrite_existing_control(self):
        # 参数名与无参服务同名时，保留先推导出的滑块（防覆盖守卫）
        services = {"thermostat": {
            "hvac": {"fields": ["cool"]},
            "cool": {"fields": ["entity_id"]},
        }}
        entity = {"entity_id": "thermostat.x", "state": "idle",
                  "attributes": {"cool": 30}}
        controls = resolve_controls(entity, services)
        assert controls["cool"]["type"] == "slider"

    def test_media_player_action_filtered_by_concept_mismatch(self):
        # play_media 的词全不匹配，且去掉 "play" 后 "media" 仍是合法服务 → 跳过
        services = {"media_player": {
            "play_media": {"fields": ["entity_id"]},
            "media": {"fields": ["entity_id"]},
        }}
        entity = {"entity_id": "media_player.x", "state": "idle", "attributes": {}}
        controls = resolve_controls(entity, services)
        assert "play_media" not in controls

    def test_concept_partial_word_match_keeps_action(self):
        # "play" 是 "replay" 的子串 → 词视为匹配；声明了能力位 → play_media 保留
        services = {"media_player": {"play_media": {"fields": ["entity_id"]}}}
        entity = {"entity_id": "media_player.x", "state": "idle",
                  "attributes": {"replay": 1, "supported_features": 512}}
        controls = resolve_controls(entity, services)
        assert controls["play_media"]["type"] == "action"

    def test_concept_match_unit_helpers(self):
        assert _concept_match(["turn", "on"], {"turn", "on"}, "light", {}) is True
        assert _concept_match(["play", "media"], set(), "media_player",
                              {"media": {"fields": []}}) is False

    def test_pct_slider_backfilled_from_turn_on(self):
        # 灯关时无 brightness 属性，但属性词出现 → 反推 0-100 滑块
        services = {"light": {"turn_on": {"fields": ["brightness_pct"]}}}
        entity = {"entity_id": "light.x", "state": "off",
                  "attributes": {"brightness_min": 10}}
        controls = resolve_controls(entity, services)
        backfilled = controls["brightness"]
        assert backfilled["type"] == "slider"
        assert (backfilled["min"], backfilled["max"], backfilled["current"]) == (0, 100, 0)
        assert backfilled["param"] == "brightness_pct"

    def test_section5_skips_numeric_attr_already_covered(self):
        # brightness 数值已有但第 2 节因非 pct 字段跳过 → 第 5 节也不补（103 行守卫）
        services = {"light": {
            "turn_on": {"fields": ["brightness"]},
            "set_brightness": {"fields": ["brightness"]},
        }}
        entity = {"entity_id": "light.x", "state": "on",
                  "attributes": {"brightness": 128}}
        assert resolve_controls(entity, services) == {}

    def test_enum_target_falls_back_to_pct_field(self):
        services = {"fan": {"turn_on": {"fields": ["oscillate_pct"]}}}
        entity = {"entity_id": "fan.x", "state": "on",
                  "attributes": {"oscillates": ["left", "right"]}}
        controls = resolve_controls(entity, services)
        assert controls["oscillate"]["param"] == "oscillate_pct"

    def test_slider_via_attr_name_part_match(self):
        # current_humidity 无精确字段 → 用属性词命中 target_humidity 字段
        services = {"climate": {"set_climate": {"fields": ["target_humidity"]}}}
        entity = {"entity_id": "climate.x", "state": "on",
                  "attributes": {"current_humidity": 40}}
        controls = resolve_controls(entity, services)
        assert controls["current_humidity"]["type"] == "slider"
        assert controls["current_humidity"]["current"] == 40

    def test_controls_to_text_sub_name_and_indent(self):
        entity = {"entity_id": "light.sub", "attributes": {"friendly_name": "别名"}}
        text = controls_to_text(entity, {}, indent=1, sub_name="左键")
        assert "子功能 light.sub（左键）" in text
        assert "(no controls)" in text


# ===========================================================================
# device_registry 边界
# ===========================================================================

class FakeHAService:
    def __init__(self, grouped, flat, svc_defs):
        self._grouped = grouped
        self._flat = flat
        self._svc_defs = svc_defs

    async def get_all_devices_grouped(self):
        return self._grouped

    async def get_all_devices(self):
        return self._flat

    async def get_service_defs(self, ha_client, domains=None):
        return self._svc_defs


def _snapshot_fixture():
    flat = [
        {"entity_id": "light.hall", "domain": "light", "state": "on",
         "name": "A灯 会客厅灯 左键", "area_id": "a1", "area_name": "客厅",
         "attributes": {"friendly_name": "A灯 会客厅灯 左键"}},
        {"entity_id": "sensor.temp", "domain": "sensor", "state": "22",
         "name": "温度", "area_id": "a1", "area_name": "客厅", "attributes": {}},
    ]
    grouped = {"devices": [{
        "device_id": "d1", "name": "A灯", "model": "M1", "manufacturer": "MI",
        "sw_version": "1.0", "area_id": "a1", "area_name": "客厅",
        "summary": "",
        "entities": [
            {"entity_id": "light.hall", "domain": "light"},
            {"entity_id": "light.ghost", "domain": "light"},  # flat 中不存在
            {"entity_id": "sensor.temp", "domain": "sensor"},
        ],
    }]}
    return FakeHAService(grouped, flat, {"light": {"turn_on": {"fields": ["entity_id"]}}})


class TestDeviceRegistryEdges:
    def test_derive_sub_name_edge_cases(self):
        assert derive_sub_name("", "设备") == ""
        assert derive_sub_name("设备", "设备") == ""
        assert derive_sub_name("", "") == ""
        assert derive_sub_name("客厅灯", "A灯") == "客厅灯"  # 不含父名前缀 → 原样

    def test_entry_label_fallbacks(self):
        assert entry_label({"sub_name": "左键", "device_name": "A灯"}) == "A灯 左键"
        assert entry_label({"name": "", "entity_id": "light.x"}) == "light.x"

    async def test_snapshot_skips_entity_missing_from_flat(self, monkeypatch):
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod.Database, "_instance", None)  # _load_scope 吞错返回 {}
        ha = _snapshot_fixture()
        snap = await build_device_snapshot(ha, MagicMock())
        eids = [e["entity_id"] for e in snap["entries"]]
        assert eids == ["light.hall"]  # ghost 与 sensor 都不进 entries
        assert snap["service_defs"] == ha._svc_defs

    async def test_snapshot_flip_failure_falls_back_to_raw_state(self, monkeypatch):
        import app.services.semantic_map as sm_mod
        async def boom(eid, state):
            raise RuntimeError("db down")
        monkeypatch.setattr(sm_mod, "flip_state_value", boom)
        ha = _snapshot_fixture()
        snap = await build_device_snapshot(ha, MagicMock())
        entry = snap["entries"][0]
        assert entry["state"] == "on"  # 翻转失败 → 原始 state

    def test_render_catalog_empty(self):
        assert render_catalog_text({"devices": []}) == "(暂无 HA 设备)"

    def test_render_controls_skips_empty_devices_and_controls(self):
        snap = {
            "devices": [
                {"name": "空设备", "visible_entries": []},
                {"name": "无控件", "visible_entries": [
                    {"entity_id": "light.x", "domain": "light", "state": "on",
                     "controls": {}, "note": "", "sub_name": "",
                     "attributes": {"friendly_name": "X"}},
                ]},
            ],
        }
        assert render_controls_text(snap) == ""

    def test_render_devices_brief_and_entities_flat(self):
        snap = {
            "entries": [{
                "entity_id": "light.hall", "domain": "light", "name": "n",
                "state": "on", "attributes": {}, "area_id": "a1",
                "area_name": "客厅", "controls": {}, "note": "备注",
            }],
            "devices": [{
                "name": "A灯", "model": "M1", "area_name": "客厅", "summary": "s",
                "visible_entries": [{
                    "entity_id": "light.hall", "domain": "light", "name": "n",
                    "state": "on", "attributes": {}, "controls": {},
                }],
            }],
        }
        brief = render_devices_brief(snap)
        assert brief[0]["entity_labels"] == {"light.hall": "n"}
        flat = render_entities_flat(snap)
        assert flat[0]["ai_operable"] is True and flat[0]["note"] == "备注"

    async def test_snapshot_hides_prohibited_entries(self, monkeypatch):
        import app.core.database as db_mod
        db = _mock_db()

        def prefs_by_scope(scope):
            return {"light.hall": "1"} if scope == "entity_operable" else {}
        db.prefs_get_by_scope = AsyncMock(side_effect=prefs_by_scope)
        monkeypatch.setattr(db_mod.Database, "get",
                            classmethod(lambda cls: db))
        ha = _snapshot_fixture()
        snap = await build_device_snapshot(ha, MagicMock())
        assert snap["entries"] == []
        assert snap["devices"][0]["visible_entries"] == []
        assert "light.hall" not in render_catalog_text(snap)


# ===========================================================================
# semantic_map
# ===========================================================================

@pytest.fixture
def reset_semantic_cache(monkeypatch):
    import app.services.semantic_map as sm
    sm._cache.clear()
    sm._cache_loaded = False
    yield sm
    sm._cache.clear()
    sm._cache_loaded = False


class TestSemanticMapEdges:
    async def test_reload_with_invalid_json_warns_and_skips(self, reset_semantic_cache,
                                                            monkeypatch):
        sm = reset_semantic_cache
        db = MagicMock()
        db.prefs_get_by_scope = AsyncMock(
            return_value={"light.x": "{not-json", "light.y": "[]"})  # 一条坏 JSON 一条无 mappings
        monkeypatch.setattr("app.core.database.Database.get", classmethod(
            lambda cls: db))
        result = await get_action_map("light.x")
        assert result is None
        assert sm._cache_loaded is True

    async def test_reload_db_failure_returns_none(self, reset_semantic_cache,
                                                  monkeypatch):
        sm = reset_semantic_cache
        def boom(cls):
            raise RuntimeError("db down")
        monkeypatch.setattr("app.core.database.Database.get", classmethod(boom))
        assert await get_action_map("light.x") is None

    async def test_apply_state_flip_non_on_off_state_unchanged(self, reset_semantic_cache):
        sm = reset_semantic_cache
        sm._cache["light.x"] = {"mappings": {
            "turn_on": {"target": "turn_off"},
            "turn_off": {"target": "turn_on"},
        }}
        sm._cache_loaded = True
        state = {"state": "unavailable", "attributes": {}}
        assert apply_state_flip(state, "light.x") is state  # 非 on/off 原样返回

    def test_invalidate_cache(self, reset_semantic_cache):
        sm = reset_semantic_cache
        sm._cache_loaded = True
        invalidate_cache()
        assert sm._cache_loaded is False


# ===========================================================================
# health_check
# ===========================================================================

class TestHealthCheckEdges:
    async def test_check_ha_timeout_marks_unavailable(self):
        hc = HealthChecker()
        client = MagicMock()
        client.get_states = AsyncMock(side_effect=asyncio.TimeoutError())
        assert await hc.check_ha(client) is False
        assert hc.get_status() == {"ha_available": False, "llm_available": False}

    async def test_check_ha_empty_states_marks_unavailable(self):
        hc = HealthChecker()
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        assert await hc.check_ha(client) is False  # 0 实体 → 不可用

    async def test_check_ha_generic_error_marks_unavailable(self):
        hc = HealthChecker()
        client = MagicMock()
        client.get_states = AsyncMock(side_effect=RuntimeError("conn refused"))
        assert await hc.check_ha(client) is False

    async def test_check_llm_disabled(self):
        hc = HealthChecker(ha_available=True)
        client = MagicMock()
        client.enabled = False
        assert await hc.check_llm(client) is False
        assert hc.ha_available is True  # 不影响另一项

    async def test_check_llm_timeout_and_error(self):
        for exc in (asyncio.TimeoutError(), RuntimeError("boom")):
            hc = HealthChecker()
            client = MagicMock()
            client.enabled = True
            client.chat = AsyncMock(side_effect=exc)
            assert await hc.check_llm(client) is False

    async def test_check_llm_empty_response(self):
        hc = HealthChecker()
        client = MagicMock()
        client.enabled = True
        client.chat = AsyncMock(return_value=None)
        assert await hc.check_llm(client) is False

    async def test_check_all_aggregates(self):
        hc = HealthChecker()
        ha = MagicMock()
        ha.ping = AsyncMock(return_value=True)
        llm = MagicMock()
        llm.enabled = True
        llm.chat = AsyncMock(return_value="ok")
        status = await hc.check_all(ha, llm)
        assert status == {"ha": True, "llm": True}
