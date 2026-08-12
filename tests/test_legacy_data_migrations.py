"""Tests for ``app/migrations.py`` —— 启动期历史数据迁移的编排逻辑。

这些编排此前内联在 ``main.py`` 的 lifespan 里且无任何测试覆盖。抽到独立模块后，
这里覆盖触发条件与容错语义（单块失败只 warning，不阻塞）：

- migrate_global_llm_keys: config.json 已有 keys 不迁移；空则从首个 user DB 迁移
- migrate_home_info: home 段已 complete 不迁移；否则从 user DB 镜像到 config.json
- load_vision_focuses: KV 有新多条格式直接 load；仅有旧单条则迁成新格式并回写
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


# ==================== migrate_global_llm_keys ====================

class TestMigrateGlobalLlmKeys:
    @pytest.mark.asyncio
    async def test_skips_when_config_already_has_keys(self):
        """config.json 已有 llm_keys：不查 user DB，不迁移。"""
        import app.core.config as cfg
        from app.migrations import migrate_global_llm_keys

        cfg.CONFIG["llm_keys"] = [{"id": "existing"}]
        db = MagicMock()
        db.user_list_all = AsyncMock()

        await migrate_global_llm_keys(db)

        db.user_list_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_migrates_from_first_user_db_when_config_empty(self):
        """config.json 空 + 某 user DB 有 llm_keys：迁移并落盘，明文 api_key 被剥离。"""
        import app.core.config as cfg
        from app.migrations import migrate_global_llm_keys

        cfg.CONFIG["llm_keys"] = []
        llm_keys = [{"id": "k1", "type": "chat", "api_key": "secret-1"}]
        providers = {"chat": {"key_id": "k1"}}

        db = MagicMock()
        db.user_list_all = AsyncMock(return_value=[{"id": "u1", "username": "alice"}])
        # 依次返回 llm_keys 与 providers 两个 setting
        db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),
            json.dumps(providers),
        ])

        await migrate_global_llm_keys(db)

        # 迁移后内存 CONFIG 含迁来的 key（明文 api_key 被 save_global_llm_keys 剥离）
        assert len(cfg.CONFIG["llm_keys"]) == 1
        assert cfg.CONFIG["llm_keys"][0]["id"] == "k1"
        assert "api_key" not in cfg.CONFIG["llm_keys"][0]
        # providers 一并迁到全局
        assert cfg.CONFIG["providers"] == providers

    @pytest.mark.asyncio
    async def test_no_keys_anywhere_is_noop(self):
        """config 空 + 所有 user DB 都无 llm_keys：不迁移，CONFIG 保持空。"""
        import app.core.config as cfg
        from app.migrations import migrate_global_llm_keys

        cfg.CONFIG["llm_keys"] = []
        db = MagicMock()
        db.user_list_all = AsyncMock(return_value=[{"id": "u1", "username": "alice"}])
        db.user_setting_get = AsyncMock(return_value=None)  # 无 llm_keys

        await migrate_global_llm_keys(db)

        assert cfg.CONFIG["llm_keys"] == []

    @pytest.mark.asyncio
    async def test_db_error_does_not_raise(self):
        """外层 try/except：db 异常只 warning，不向上抛。"""
        from app.migrations import migrate_global_llm_keys

        db = MagicMock()
        db.user_list_all = AsyncMock(side_effect=RuntimeError("db down"))

        await migrate_global_llm_keys(db)  # 不抛即通过


# ==================== migrate_home_info ====================

class TestMigrateHomeInfo:
    @pytest.mark.asyncio
    async def test_skips_when_home_already_complete(self):
        """config 的 home 段已有 city/district：不迁移。"""
        import app.core.config as cfg
        from app.migrations import migrate_home_info

        cfg.CONFIG["home"] = {"city": "Shanghai"}
        db = MagicMock()
        db.user_list_all = AsyncMock()

        await migrate_home_info(db)

        db.user_list_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_migrates_home_info_from_user_db(self):
        """home 段缺失 + user DB 有 home_info：镜像到 config.json 的 home 段。"""
        import app.core.config as cfg
        from app.migrations import migrate_home_info

        cfg.CONFIG["home"] = {}
        home_info = {
            "home_name": "我家", "owner_name": "alice",
            "province": "北京", "city": "Beijing", "district": "海淀",
        }
        db = MagicMock()
        db.user_list_all = AsyncMock(return_value=[{"id": "u1", "username": "alice"}])
        db.user_setting_get = AsyncMock(return_value=json.dumps(home_info))

        await migrate_home_info(db)

        assert cfg.CONFIG["home"]["city"] == "Beijing"
        assert cfg.CONFIG["home"]["district"] == "海淀"
        assert cfg.CONFIG["home"]["home_name"] == "我家"

    @pytest.mark.asyncio
    async def test_skips_user_with_incomplete_home_info(self):
        """user DB 的 home_info 缺 city/district：跳过该 user，不写空 home。"""
        import app.core.config as cfg
        from app.migrations import migrate_home_info

        cfg.CONFIG["home"] = {}
        db = MagicMock()
        db.user_list_all = AsyncMock(return_value=[{"id": "u1", "username": "alice"}])
        db.user_setting_get = AsyncMock(return_value=json.dumps({"home_name": "x"}))  # 无 city/district

        await migrate_home_info(db)

        # 未迁移：home 段不被写，仍为空 dict
        assert cfg.CONFIG.get("home") == {}

    @pytest.mark.asyncio
    async def test_db_error_does_not_raise(self):
        from app.migrations import migrate_home_info

        db = MagicMock()
        db.user_list_all = AsyncMock(side_effect=RuntimeError("db down"))

        await migrate_home_info(db)  # 外层 try/except 吞掉


# ==================== load_vision_focuses ====================

class TestLoadVisionFocuses:
    @pytest.mark.asyncio
    async def test_loads_when_new_format_kv_exists(self):
        """KV 有 vision_focuses（新多条格式）：直接 load，不走迁移。"""
        from app.migrations import load_vision_focuses

        focuses = [{"text": "detect person", "camera_id": ""}]
        db = MagicMock()
        db.kv_get = AsyncMock(return_value=json.dumps(focuses))
        vision_service = MagicMock()

        await load_vision_focuses(db, vision_service)

        vision_service.load_focuses.assert_called_once_with(focuses)
        vision_service.add_focus.assert_not_called()
        db.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_migrates_old_single_focus_when_new_kv_absent(self):
        """新 KV 缺失 + 存在旧单条 vision_focus：迁成新格式并回写 KV。"""
        from app.migrations import load_vision_focuses

        db = MagicMock()
        # kv_get 依次：vision_focuses=None, vision_focus="detect motion at door"
        db.kv_get = AsyncMock(side_effect=[None, "detect motion at door"])
        db.kv_set = AsyncMock()
        vision_service = MagicMock()
        migrated_focuses = [{"text": "detect motion at door", "camera_id": ""}]
        vision_service.get_vision_focuses = MagicMock(return_value=migrated_focuses)

        await load_vision_focuses(db, vision_service)

        vision_service.add_focus.assert_called_once_with("detect motion at door")
        db.kv_set.assert_called_once_with("vision_focuses", json.dumps(migrated_focuses))

    @pytest.mark.asyncio
    async def test_no_focuses_anywhere_does_nothing(self):
        """新旧 KV 都缺失：什么都不做。"""
        from app.migrations import load_vision_focuses

        db = MagicMock()
        db.kv_get = AsyncMock(side_effect=[None, None])
        vision_service = MagicMock()

        await load_vision_focuses(db, vision_service)

        vision_service.load_focuses.assert_not_called()
        vision_service.add_focus.assert_not_called()
        db.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_error_propagates_no_outer_guard(self):
        """load_vision_focuses 无外层 try/except（与 sibling 不同，保留现状）：db 异常向上抛。"""
        from app.migrations import load_vision_focuses

        db = MagicMock()
        db.kv_get = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(RuntimeError):
            await load_vision_focuses(db, MagicMock())
