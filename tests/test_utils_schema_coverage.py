"""补充覆盖测试：utils / schema / migrations 层此前未覆盖的分支。

目标模块：
- app/utils/text_match.py — fuzzy_match / 去重 / 域名降权排序
- app/utils/file_utils.py — 原子写入失败兜底
- app/utils/json_extractor.py — 括号计数 / extract_json_object 三层解析
- app/utils/async_utils.py — 任务异常留痕 / shutdown
- app/schema/api_schemas.py — 各 field_validator 接受/拒绝
- app/migrations.py — 启动迁移的容错分支
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.utils.async_utils import TaskManager, create_task_manager
from app.utils.file_utils import atomic_write
from app.utils.json_extractor import (
    _find_balanced_json,
    extract_json_from_content,
    extract_json_object,
)
from app.utils.text_match import fuzzy_match, match_devices


# ===========================================================================
# text_match
# ===========================================================================

class TestFuzzyMatch:
    def test_empty_inputs(self):
        assert fuzzy_match("", "target") is False
        assert fuzzy_match("q", "") is False

    def test_substring_both_directions(self):
        assert fuzzy_match("客厅灯", "客厅吊灯") is True     # query in target
        assert fuzzy_match("客厅吊灯", "客厅") is True       # target in query
        assert fuzzy_match("卧室灯", "客厅吊灯") is False

    def test_bigram_fallback(self):
        # 与 target 无子串关系，但 query 的 2-gram「顶盒」出现在 target
        assert fuzzy_match("机顶盒", "顶盒设备") is True
        assert fuzzy_match(" xyz ", "abc") is False


class TestMatchDevicesEdge:
    def _dev(self, eid, name, area=""):
        return {"entity_id": eid, "name": name, "area_name": area}

    def test_duplicate_entity_ids_are_deduped(self):
        devices = [self._dev("light.x", "客厅灯"),
                   self._dev("light.x", "客厅灯")]  # 同 id 重复出现
        result = match_devices("客厅灯", devices)
        assert [d["entity_id"] for d in result] == ["light.x"]

    def test_ranking_primary_before_diagnostic(self):
        devices = [
            self._dev("sensor.door", "大门传感器"),      # 诊断 domain → base 2
            self._dev("timer.kitchen", "大门计时器"),    # 其他 domain → base 1
            self._dev("lock.door", "大门锁"),            # 主控 domain → base 0
        ]
        result = match_devices("大门", devices)
        assert [d["entity_id"] for d in result] == [
            "lock.door", "timer.kitchen", "sensor.door"]

    def test_exact_name_match_beats_superstring(self):
        devices = [
            self._dev("switch.hall", "大门开关故障"),
            self._dev("switch.door", "大门开关"),
        ]
        result = match_devices("大门开关", devices)
        assert result[0]["entity_id"] == "switch.door"  # 同 domain 下精确名优先

    def test_empty_query_or_devices(self):
        assert match_devices("", [self._dev("light.x", "灯")]) == []
        assert match_devices("灯", []) == []


# ===========================================================================
# file_utils
# ===========================================================================

class TestAtomicWriteFailures:
    def test_os_replace_failure_falls_back_to_direct_write(self, tmp_path, monkeypatch):
        target = tmp_path / "cfg.json"
        target.write_text("old", encoding="utf-8")

        def replace_boom(src, dst):
            raise OSError("Device or resource busy")
        monkeypatch.setattr(os, "replace", replace_boom)
        atomic_write(target, "new-content")
        assert target.read_text(encoding="utf-8") == "new-content"
        assert not (tmp_path / "cfg.json.tmp").exists()  # 临时文件已清理

    def test_replace_and_direct_write_both_fail_raises(self, tmp_path, monkeypatch):
        target = tmp_path / "cfg.json"
        orig_write_text = Path.write_text

        def fake_write_text(self, data, encoding=None):
            if str(self).endswith(".tmp"):
                return orig_write_text(self, data, encoding=encoding)
            raise OSError("target locked")
        monkeypatch.setattr(os, "replace", lambda src, dst: (_ for _ in ()).throw(
            OSError("busy")))
        monkeypatch.setattr(Path, "write_text", fake_write_text)
        with pytest.raises(OSError):
            atomic_write(target, "data")
        monkeypatch.setattr(Path, "write_text", orig_write_text)  # 保险
        assert not (tmp_path / "cfg.json.tmp").exists()  # tmp 清理后再 raise

    def test_success_path_removes_nothing_extra(self, tmp_path):
        target = tmp_path / "a.txt"
        atomic_write(target, "hello")
        assert target.read_text(encoding="utf-8") == "hello"
        assert not (tmp_path / "a.txt.tmp").exists()  # 临时文件已被 rename 走

    def test_tmp_cleanup_failure_is_swallowed(self, tmp_path, monkeypatch):
        target = tmp_path / "cfg.json"
        target.write_text("old", encoding="utf-8")

        def replace_boom(src, dst):
            raise OSError("busy")

        def unlink_boom(self, missing_ok=False):
            raise OSError("unlink denied")
        monkeypatch.setattr(os, "replace", replace_boom)
        monkeypatch.setattr(Path, "unlink", unlink_boom)
        atomic_write(target, "new-content")  # unlink 失败被吞，直写仍成功
        assert target.read_text(encoding="utf-8") == "new-content"


# ===========================================================================
# json_extractor
# ===========================================================================

class TestJsonExtractorEdge:
    def test_find_balanced_json_unbalanced_returns_none(self):
        assert _find_balanced_json('{"a": 1') is None       # 括号不闭合
        assert _find_balanced_json("no braces here") is None

    def test_find_balanced_json_ignores_braces_in_strings(self):
        text = 'prefix {"a": "包含 } 花括号", "b": {"c": 1}} suffix'
        assert _find_balanced_json(text) == '{"a": "包含 } 花括号", "b": {"c": 1}}'

    def test_extract_content_balanced_but_invalid_returns_original(self):
        content = '前缀 {"a": } 后缀'
        assert extract_json_from_content(content) == content

    def test_extract_object_direct(self):
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_extract_object_markdown_block(self):
        text = '说明\n```json\n{"a": 1, "b": [2]}\n```\n完'
        assert extract_json_object(text) == {"a": 1, "b": [2]}

    def test_extract_object_greedy_braces(self):
        text = '结果 {"a": {"b": 2}} 结束'
        assert extract_json_object(text) == {"a": {"b": 2}}

    def test_extract_object_total_failure_raises(self):
        with pytest.raises(ValueError, match="无法从模型输出解析 JSON"):
            extract_json_object("完全没有 JSON")


# ===========================================================================
# async_utils
# ===========================================================================

class TestTaskManagerEdge:
    async def test_crashed_task_is_logged_and_dropped(self, caplog):
        async def boom():
            raise ValueError("task exploded")
        tm = TaskManager()
        tm.spawn(boom(), name="crasher")
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # done 回调经 call_soon 异步执行
        assert tm.pending_count == 0
        assert "Background task crashed" in caplog.text

    async def test_cancelled_task_is_logged_and_dropped(self, caplog):
        caplog.set_level(logging.INFO)
        tm = TaskManager()
        task = tm.spawn(asyncio.sleep(10), name="long-runner")
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert tm.pending_count == 0
        assert "Background task cancelled" in caplog.text

    async def test_spawn_with_on_done_callback(self):
        tm = TaskManager()
        seen = []
        task = tm.spawn(asyncio.sleep(0), on_done=lambda t: seen.append(t.get_name()))
        await task
        await asyncio.sleep(0)
        assert seen == [task.get_name()]

    async def test_shutdown_cancels_pending_tasks(self):
        tm = TaskManager()
        finished = []

        async def worker():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise
        tm.spawn(worker(), name="w1")
        tm.spawn(worker(), name="w2")
        await asyncio.sleep(0)  # 让任务先启动，取消才会进入 except 分支
        await tm.shutdown(timeout=1.0)
        assert finished == ["cancelled", "cancelled"]
        await asyncio.sleep(0)  # 等 done 回调清空跟踪表
        assert tm.pending_count == 0

    async def test_shutdown_warns_for_tasks_ignoring_cancel(self, caplog):
        caplog.set_level(logging.WARNING)
        tm = TaskManager()
        release = asyncio.Event()

        async def stubborn():
            # 吞掉取消，模拟卡死的后台任务；release 置位后自行退出
            # （无限吞取消的协程无法被外部杀死，清理必须靠它自己配合）
            while not release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass
        tm.spawn(stubborn(), name="stubborn")
        await asyncio.sleep(0)  # 让任务先启动
        await tm.shutdown(timeout=0.05)
        assert "did not finish in time" in caplog.text
        # 收尾：让任务退出，避免泄漏到事件循环关闭
        release.set()
        for t in list(tm._tasks):
            t.cancel()
        await asyncio.wait_for(asyncio.gather(*list(tm._tasks), return_exceptions=True), timeout=2)

    async def test_shutdown_with_no_tasks_returns_immediately(self):
        tm = TaskManager()
        await asyncio.wait_for(tm.shutdown(), timeout=0.1)  # 空表 → 直接返回

    async def test_pending_count_and_factory(self):
        tm = create_task_manager()
        task = tm.spawn(asyncio.sleep(0.01), name="t")
        assert tm.pending_count == 1
        await task
        await asyncio.sleep(0)
        assert tm.pending_count == 0


# ===========================================================================
# api_schemas
# ===========================================================================

class TestApiSchemaValidators:
    def test_weather_host_valid(self):
        from app.schema.api_schemas import WeatherConfigRequest
        req = WeatherConfigRequest(host="api.qweatherapi.com", kid="k1", sub="free")
        assert req.host == "api.qweatherapi.com"

    @pytest.mark.parametrize("bad_host", [
        "http://api.qweatherapi.com",  # 带协议
        "localhost",                   # 无点分
        "和风.api.com",                 # 中文
    ])
    def test_weather_host_invalid_rejected(self, bad_host):
        from app.schema.api_schemas import WeatherConfigRequest
        with pytest.raises(ValidationError):
            WeatherConfigRequest(host=bad_host, kid="k1", sub="free")

    def test_exa_api_key_length_guard(self):
        from app.schema.api_schemas import ExaConfig
        assert ExaConfig(api_key="").api_key == ""           # 空放行
        long_key = "a" * 20
        assert ExaConfig(api_key=long_key).api_key == long_key
        with pytest.raises(ValidationError, match="长度异常"):
            ExaConfig(api_key="short-key")

    def test_vision_rtsp_url_guard(self):
        from app.schema.api_schemas import VisionConfig
        assert VisionConfig(rtsp_url="").rtsp_url == ""      # 空 = 走 USB
        ok = "rtsp://user:pw@cam/stream"
        assert VisionConfig(rtsp_url=ok).rtsp_url == ok
        with pytest.raises(ValidationError, match="rtsp"):
            VisionConfig(rtsp_url="http://cam/stream")

    def test_ha_token_jwt_guard(self):
        from app.schema.api_schemas import HAConfigRequest
        assert HAConfigRequest(url="http://ha:8123").token == ""  # 空 = 不修改
        jwt = "eyJhbGciOiJIUzI1.abc-DEF_123.ghiJKL_456"
        assert HAConfigRequest(url="http://ha:8123", token=jwt).token == jwt
        with pytest.raises(ValidationError, match="JWT"):
            HAConfigRequest(url="http://ha:8123", token="一整段中文说明文字")

    def test_emoji_preference_strips_and_coerces_non_str(self):
        from app.schema.api_schemas import EmojiPreferenceRequest
        req = EmojiPreferenceRequest(scope="  chat  ", key=" k ", emoji_char=" 😀 ")
        assert (req.scope, req.key, req.emoji_char) == ("chat", "k", "😀")
        req2 = EmojiPreferenceRequest(scope=123, key=True, emoji_char=7)  # 非 str → str()
        assert (req2.scope, req2.key, req2.emoji_char) == ("123", "True", "7")

    def test_entity_alias_strips_and_coerces(self):
        from app.schema.api_schemas import EntityAliasRequest
        req = EntityAliasRequest(entity_id=" light.x ", alias=42)
        assert req.entity_id == "light.x" and req.alias == "42"

    def test_entity_note_strips_and_coerces(self):
        from app.schema.api_schemas import EntityNoteRequest
        req = EntityNoteRequest(entity_id=" light.x ", note=99)
        assert req.entity_id == "light.x" and req.note == "99"

    def test_entity_operable_strips_and_coerces(self):
        from app.schema.api_schemas import EntityOperableRequest
        req = EntityOperableRequest(entity_id=" light.x ", operable=False)
        assert req.entity_id == "light.x" and req.operable is False

    def test_action_map_strips_and_coerces(self):
        from app.schema.api_schemas import ActionMapRequest
        req = ActionMapRequest(entity_id=" light.x ")
        assert req.entity_id == "light.x" and req.mappings == {}


# ===========================================================================
# migrations
# ===========================================================================

def _migration_db(users, settings, kv=None):
    db = MagicMock()
    db.user_list_all = AsyncMock(return_value=users)
    db.user_setting_get = AsyncMock(side_effect=lambda uid, key: settings.get((uid, key)))
    db.kv_get = AsyncMock(side_effect=lambda key: (kv or {}).get(key))
    db.kv_set = AsyncMock()
    db.cameras_remap_frame_interval = AsyncMock(return_value=0)
    return db


class TestMigrations:
    async def test_llm_keys_config_hit_skips_db(self, monkeypatch):
        from app.migrations import migrate_global_llm_keys
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config",
                            lambda k, d=None: [{"id": "k1"}] if k == "llm_keys" else d)
        db = _migration_db([], {})
        await migrate_global_llm_keys(db)
        db.user_list_all.assert_not_awaited()

    async def test_llm_keys_migrated_from_first_user_db(self, monkeypatch):
        from app.migrations import migrate_global_llm_keys
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config", lambda k, d=None: d if k == "llm_keys" else [])
        saved, sections = [], {}
        monkeypatch.setattr(mig, "save_global_llm_keys", lambda keys: saved.append(keys))
        monkeypatch.setattr(mig, "update_memory_config", lambda k, v: sections.__setitem__(k, v))
        monkeypatch.setattr(mig, "update_config_section",
                            lambda k, v: sections.__setitem__(f"section:{k}", v))
        db = _migration_db(
            users=[{"id": "u1", "username": "alice"}, {"id": "u2", "username": "bob"}],
            settings={
                ("u1", "llm_keys"): json.dumps([{"id": "legacy"}]),
                ("u1", "providers"): json.dumps({"chat": {"model": "m"}}),
            },
        )
        await migrate_global_llm_keys(db)
        assert saved == [[{"id": "legacy"}]]
        assert sections["llm_keys"] == [{"id": "legacy"}]
        assert sections["providers"] == {"chat": {"model": "m"}}
        assert sections["section:providers"] == {"chat": {"model": "m"}}
        # 命中第一个有 key 的用户后即 break，providers 查询发生在同一用户上
        db.user_setting_get.assert_any_await("u1", "providers")

    async def test_llm_keys_skips_users_without_keys(self, monkeypatch):
        from app.migrations import migrate_global_llm_keys
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config", lambda k, d=None: d if k == "llm_keys" else [])
        saved = []
        monkeypatch.setattr(mig, "save_global_llm_keys", lambda keys: saved.append(keys))
        monkeypatch.setattr(mig, "update_memory_config", lambda k, v: None)
        monkeypatch.setattr(mig, "update_config_section", lambda k, v: None)
        db = _migration_db(
            users=[{"id": "u1", "username": "nokey"}, {"id": "u2", "username": "haskey"}],
            settings={
                ("u1", "llm_keys"): None,                 # 无该设置 → continue
                ("u1", "providers"): None,
                ("u2", "llm_keys"): json.dumps([]),       # 空 key 列表 → continue
                ("u2", "providers"): None,
                ("u3", "llm_keys"): json.dumps([{"id": "k"}]),
                ("u3", "providers"): None,
            },
        )
        db.user_list_all = AsyncMock(return_value=[
            {"id": "u1", "username": "nokey"},
            {"id": "u2", "username": "empty"},
            {"id": "u3", "username": "real"},
        ])
        await migrate_global_llm_keys(db)
        assert saved == [[{"id": "k"}]]

    async def test_llm_keys_persist_failure_still_migrates_memory(self, monkeypatch):
        from app.migrations import migrate_global_llm_keys
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config", lambda k, d=None: d if k == "llm_keys" else [])
        mem = {}

        def save_boom(keys):
            raise OSError("config.json readonly")
        monkeypatch.setattr(mig, "save_global_llm_keys", save_boom)
        monkeypatch.setattr(mig, "update_memory_config", lambda k, v: mem.__setitem__(k, v))
        monkeypatch.setattr(mig, "update_config_section", lambda k, v: None)
        db = _migration_db(
            users=[{"id": "u1", "username": "alice"}],
            settings={("u1", "llm_keys"): json.dumps([{"id": "k"}]),
                      ("u1", "providers"): None},
        )
        await migrate_global_llm_keys(db)  # 持久化失败只告警，不外抛
        assert mem["llm_keys"] == [{"id": "k"}]

    async def test_llm_keys_unexpected_error_swallowed(self, monkeypatch):
        from app.migrations import migrate_global_llm_keys
        import app.migrations as mig
        def boom(k, d=None):
            raise RuntimeError("config shattered")
        monkeypatch.setattr(mig, "get_config", boom)
        await migrate_global_llm_keys(MagicMock())  # 外层 try 兜住

    async def test_home_info_skipped_when_config_complete(self, monkeypatch):
        from app.migrations import migrate_home_info
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config",
                            lambda k, d=None: {"city": "杭州"} if k == "home" else d)
        sections = []
        monkeypatch.setattr(mig, "update_config_section", lambda k, v: sections.append(k))
        db = _migration_db([{"id": "u1", "username": "a"}], {})
        await migrate_home_info(db)
        assert sections == []
        db.user_list_all.assert_not_awaited()

    async def test_home_info_migrated_from_user_db(self, monkeypatch):
        from app.migrations import migrate_home_info
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config", lambda k, d=None: d if k == "home" else {})
        sections = {}
        monkeypatch.setattr(mig, "update_config_section",
                            lambda k, v: sections.__setitem__(k, v))
        db = _migration_db(
            users=[{"id": "u1", "username": "a"}],
            settings={("u1", "home_info"): json.dumps({
                "home_name": "家", "owner_name": "我", "province": "浙江",
                "city": "杭州", "district": "西湖"})},
        )
        await migrate_home_info(db)
        assert sections["home"]["city"] == "杭州"
        assert sections["home"]["district"] == "西湖"

    async def test_home_info_invalid_json_skipped(self, monkeypatch):
        from app.migrations import migrate_home_info
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config", lambda k, d=None: d if k == "home" else {})
        sections = {}
        monkeypatch.setattr(mig, "update_config_section",
                            lambda k, v: sections.__setitem__(k, v))
        db = _migration_db(
            users=[{"id": "u1", "username": "a"}, {"id": "u2", "username": "b"}],
            settings={
                ("u1", "home_info"): "{broken json",       # 坏 JSON → continue
                ("u2", "home_info"): json.dumps({"city": ""}),  # 无城市 → continue
            },
        )
        await migrate_home_info(db)
        assert sections == {}

    async def test_home_info_db_error_swallowed(self, monkeypatch):
        from app.migrations import migrate_home_info
        import app.migrations as mig
        monkeypatch.setattr(mig, "get_config", lambda k, d=None: d if k == "home" else {})
        db = MagicMock()
        db.user_list_all = AsyncMock(side_effect=RuntimeError("db gone"))
        await migrate_home_info(db)  # 外层 try 兜住

    async def test_load_vision_focuses_invalid_json_keeps_service_clean(self, monkeypatch):
        from app.migrations import load_vision_focuses
        vision = MagicMock()
        db = _migration_db([], {}, kv={"vision_focuses": "{not json"})
        await load_vision_focuses(db, vision)
        vision.load_focuses.assert_not_called()

    async def test_load_vision_focuses_valid_json_loads(self, monkeypatch):
        from app.migrations import load_vision_focuses
        vision = MagicMock()
        db = _migration_db([], {}, kv={"vision_focuses": json.dumps(
            [{"id": "a", "text": "t", "enabled": True, "camera_id": ""}])})
        await load_vision_focuses(db, vision)
        vision.load_focuses.assert_called_once()

    async def test_load_vision_focuses_old_single_field_migrated(self, monkeypatch):
        from app.migrations import load_vision_focuses
        vision = MagicMock()
        vision.get_vision_focuses.return_value = [{"id": "a"}]
        db = _migration_db([], {}, kv={"vision_focus": "门口有人"})  # 无新键有旧键
        await load_vision_focuses(db, vision)
        vision.add_focus.assert_called_once_with("门口有人")
        db.kv_set.assert_awaited_once()

    async def test_migrate_camera_frame_interval_noop(self):
        from app.migrations import migrate_camera_frame_interval
        db = _migration_db([], {})
        db.cameras_remap_frame_interval = AsyncMock(return_value=0)
        await migrate_camera_frame_interval(db)  # changed=0 → 无日志，不抛
