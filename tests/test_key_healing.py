"""Tests for 启动自愈 _heal_global_keys_from_user_db。

全局 llm_keys 中无效的角色 key（空/占位符），从 per-user DB 找第一个
有该角色有效明文 api_key 的用户条目恢复。场景：wizard 把 embed/vision
key 同时写进全局 .env（env 引用）和 per-user DB（明文）；容器重建后
.env 丢失/占位符 → 全局解析为空，但 per-user DB 的明文 key 还在。
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestIsApiKeyValid:
    """测试 is_api_key_valid 占位符检测。"""

    def test_empty_is_invalid(self):
        from app.core.key_healing import is_api_key_valid
        assert is_api_key_valid("") is False
        assert is_api_key_valid("   ") is False

    def test_placeholder_is_invalid(self):
        from app.core.key_healing import is_api_key_valid
        assert is_api_key_valid("your_siliconflow_api_key_here") is False
        assert is_api_key_valid("your-zhipu-key") is False
        assert is_api_key_valid("YOUR_EMBED_KEY_HERE") is False

    def test_real_key_is_valid(self):
        from app.core.key_healing import is_api_key_valid
        assert is_api_key_valid("sk-gvibcvl1234567890") is True
        assert is_api_key_valid("sk-real-key") is True


class TestHealGlobalKeysFromUserDb:
    """测试 heal_global_keys_from_user_db 从 per-user DB 恢复无效全局 key。"""

    @pytest.mark.asyncio
    async def test_heals_invalid_embed_key_from_user_db(self):
        from app.core.key_healing import heal_global_keys_from_user_db

        # 全局 llm_keys：embed 的 api_key_env 指向的 env 是占位符
        global_keys = [
            {"id": "k1", "type": "chat", "api_key": "sk-chat-real", "api_key_env": "CHAT_ENV"},
            {"id": "k2", "type": "embed", "api_key_env": "LLM_KEY_API_C12091"},
        ]
        # per-user DB：embed 有明文 api_key
        user_keys = [
            {"id": "k1", "type": "chat", "api_key": "sk-chat-real"},
            {"id": "k2", "type": "embed", "api_key": "sk-embed-real-from-db"},
        ]
        users = [{"id": "u1", "username": "admin"}]

        mock_db = MagicMock()
        mock_db.user_list_all = AsyncMock(return_value=users)
        mock_db.user_setting_get = AsyncMock(return_value=json.dumps(user_keys))

        mem_config = {}
        env_written = {}

        def fake_update_mem(key, value):
            mem_config[key] = value

        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config", side_effect=fake_update_mem), \
             patch("app.core.config.write_secrets", side_effect=lambda e: env_written.update(e)), \
             patch("app.core.key_resolver.resolve_api_key",
                   side_effect=lambda k: k.get("api_key") or ""), \
             patch("app.core.database.Database.get", return_value=mock_db), \
             patch.dict("os.environ", {"LLM_KEY_API_C12091": "your_siliconflow_api_key_here"}, clear=False):
            await heal_global_keys_from_user_db()

        # 内存 CONFIG 的 embed 条目补了明文 api_key
        healed_keys = mem_config["llm_keys"]
        embed_entry = next(k for k in healed_keys if k["type"] == "embed")
        assert embed_entry["api_key"] == "sk-embed-real-from-db"
        # .env 写入了对应变量
        assert env_written["LLM_KEY_API_C12091"] == "sk-embed-real-from-db"
        # chat 角色有效，不被处理
        chat_entry = next(k for k in healed_keys if k["type"] == "chat")
        assert chat_entry["api_key"] == "sk-chat-real"

    @pytest.mark.asyncio
    async def test_no_heal_when_all_keys_valid(self):
        from app.core.key_healing import heal_global_keys_from_user_db

        global_keys = [
            {"id": "k1", "type": "chat", "api_key": "sk-chat-real"},
            {"id": "k2", "type": "embed", "api_key": "sk-embed-real"},
        ]

        mock_db = MagicMock()
        mock_db.user_list_all = AsyncMock(return_value=[])
        mock_db.user_setting_get = AsyncMock(return_value=None)

        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config") as mock_mem, \
             patch("app.core.config.write_secrets") as mock_env, \
             patch("app.core.key_resolver.resolve_api_key",
                   side_effect=lambda k: k.get("api_key") or ""), \
             patch("app.core.database.Database.get", return_value=mock_db):
            await heal_global_keys_from_user_db()

        # 全部有效，不写内存也不写 .env
        mock_mem.assert_not_called()
        mock_env.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_heal_when_user_db_has_no_valid_key(self):
        from app.core.key_healing import heal_global_keys_from_user_db

        global_keys = [
            {"id": "k2", "type": "embed", "api_key_env": "LLM_KEY_API_C12091"},
        ]
        # per-user DB 的 embed key 也是占位符
        user_keys = [
            {"id": "k2", "type": "embed", "api_key": "your_siliconflow_api_key_here"},
        ]
        users = [{"id": "u1", "username": "admin"}]

        mock_db = MagicMock()
        mock_db.user_list_all = AsyncMock(return_value=users)
        mock_db.user_setting_get = AsyncMock(return_value=json.dumps(user_keys))

        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config") as mock_mem, \
             patch("app.core.config.write_secrets") as mock_env, \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=mock_db):
            await heal_global_keys_from_user_db()

        # per-user DB 也没有效 key，不写
        mock_mem.assert_not_called()
        mock_env.assert_not_called()

    @pytest.mark.asyncio
    async def test_heals_multiple_roles(self):
        from app.core.key_healing import heal_global_keys_from_user_db

        global_keys = [
            {"id": "k1", "type": "embed", "api_key_env": "EMBED_ENV"},
            {"id": "k2", "type": "vision", "api_key_env": "VISION_ENV"},
            {"id": "k3", "type": "chat", "api_key": "sk-chat-real"},
        ]
        user_keys = [
            {"id": "k1", "type": "embed", "api_key": "sk-embed-healed"},
            {"id": "k2", "type": "vision", "api_key": "sk-vision-healed"},
            {"id": "k3", "type": "chat", "api_key": "sk-chat-real"},
        ]
        users = [{"id": "u1", "username": "admin"}]

        mock_db = MagicMock()
        mock_db.user_list_all = AsyncMock(return_value=users)
        mock_db.user_setting_get = AsyncMock(return_value=json.dumps(user_keys))

        mem_config = {}
        env_written = {}

        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config",
                   side_effect=lambda k, v: mem_config.__setitem__(k, v)), \
             patch("app.core.config.write_secrets",
                   side_effect=lambda e: env_written.update(e)), \
             patch("app.core.key_resolver.resolve_api_key",
                   side_effect=lambda k: k.get("api_key") or ""), \
             patch("app.core.database.Database.get", return_value=mock_db):
            await heal_global_keys_from_user_db()

        assert env_written["EMBED_ENV"] == "sk-embed-healed"
        assert env_written["VISION_ENV"] == "sk-vision-healed"
        healed_types = {k["type"] for k in mem_config["llm_keys"] if k.get("api_key", "").startswith("sk-")}
        assert healed_types == {"embed", "vision", "chat"}
