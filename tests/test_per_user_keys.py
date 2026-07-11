"""Tests for per-user key resolution and sync."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestResolveKeyForRoleUser:
    """测试 resolve_key_for_role_user — per-user key 解析。"""

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_user_id(self):
        from app.core.key_resolver import resolve_key_for_role_user
        result = await resolve_key_for_role_user("chat", "")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_user_has_no_keys(self):
        from app.core.key_resolver import resolve_key_for_role_user

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(return_value=None)
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("chat", "user-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolves_key_by_provider_binding(self):
        from app.core.key_resolver import resolve_key_for_role_user

        llm_keys = [
            {"id": "key-a", "base_url": "https://api.a.com/v1", "model": "gpt-4",
             "type": "chat", "api_key": "secret-a", "chat_path": "/chat/completions"},
        ]
        providers = {"chat": {"key_id": "key-a"}}

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),   # llm_keys
            json.dumps(providers),   # providers
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("chat", "user-1")

        assert result is not None
        assert result["api_key"] == "secret-a"
        assert result["model"] == "gpt-4"
        assert result["base_url"] == "https://api.a.com/v1"

    @pytest.mark.asyncio
    async def test_auto_selects_when_no_provider_binding(self):
        from app.core.key_resolver import resolve_key_for_role_user

        llm_keys = [
            {"id": "key-b", "base_url": "https://api.b.com/v1", "model": "claude",
             "type": "summary", "api_key": "secret-b"},
            {"id": "key-c", "base_url": "https://api.c.com/v1", "model": "gpt-4",
             "type": "chat", "api_key": "secret-c"},
        ]
        providers = {}  # 无 binding

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),
            json.dumps(providers),
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("chat", "user-1")

        assert result is not None
        assert result["api_key"] == "secret-c"
        assert result["model"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_two_users_get_different_keys(self):
        from app.core.key_resolver import resolve_key_for_role_user

        user_a_keys = [{"id": "k1", "base_url": "https://a.com", "model": "m1", "type": "chat", "api_key": "key-A"}]
        user_b_keys = [{"id": "k2", "base_url": "https://b.com", "model": "m2", "type": "chat", "api_key": "key-B"}]

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(user_a_keys), "{}",
            json.dumps(user_b_keys), "{}",
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result_a = await resolve_key_for_role_user("chat", "user-a")
            result_b = await resolve_key_for_role_user("chat", "user-b")

        assert result_a["api_key"] == "key-A"
        assert result_b["api_key"] == "key-B"


class TestSyncLlmKeysStoresPlaintext:
    """测试 _sync_llm_keys_to_current_user 存明文 api_key。"""

    @pytest.mark.asyncio
    async def test_sync_writes_plaintext_api_key(self):
        from app.routes.settings_routes import _sync_llm_keys_to_current_user

        # 全局 CONFIG 中有 key 但只有 env 名，没有明文
        global_keys = [
            {"id": "k1", "base_url": "https://a.com", "model": "m1",
             "type": "chat", "api_key_env": "MY_CHAT_KEY"},
        ]
        stored_keys = {}

        async def mock_set(user_id, key, value):
            stored_keys[key] = value

        mock_db = MagicMock()
        mock_db.user_setting_set = AsyncMock(side_effect=mock_set)

        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.database.Database.get", return_value=mock_db), \
             patch("os.getenv", return_value="plaintext-secret"):
            await _sync_llm_keys_to_current_user({"user_id": "user-1"})

        synced = json.loads(stored_keys["llm_keys"])
        assert synced[0]["api_key"] == "plaintext-secret"
        assert synced[0]["api_key_env"] == "MY_CHAT_KEY"
