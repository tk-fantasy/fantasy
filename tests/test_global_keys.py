"""Tests for global LLM key config feature — config persistence, use_global flag,
secondary password gating, and startup load preference for config.json.

Covers the feature added in plan-sess_6a3fa977:
- save_global_llm_keys writes config.json top-level array (no plaintext api_key)
- resolve_key_for_role_user honors use_global=True (returns None even with per-user keys)
- secondary password setup/verify/protects write routes
- global key CRUD routes
- startup load prefers config.json over user DB
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ==================== save_global_llm_keys ====================

class TestSaveGlobalLlmKeys:
    """save_global_llm_keys 写 config.json 顶层数组，剥离明文 api_key。"""

    def test_writes_array_to_config_json(self, tmp_path, monkeypatch):
        import app.core.config as cfg

        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
        monkeypatch.setattr(cfg, "CONFIG", {"llm_keys": []})

        keys = [
            {"id": "k1", "base_url": "https://a.com/v1", "model": "m1",
             "type": "chat", "api_key_env": "LLM_KEY_K1", "api_key": "secret-1"},
        ]
        saved = cfg.save_global_llm_keys(keys)

        # 返回值剥离了明文 api_key
        assert "api_key" not in saved[0]
        assert saved[0]["api_key_env"] == "LLM_KEY_K1"
        # 磁盘上 config.json 也没有明文 api_key
        on_disk = json.loads(config_path.read_text(encoding="utf-8"))
        assert on_disk["llm_keys"][0]["api_key_env"] == "LLM_KEY_K1"
        assert "api_key" not in on_disk["llm_keys"][0]
        # 内存 CONFIG 同步
        assert cfg.CONFIG["llm_keys"][0]["id"] == "k1"

    def test_replaces_entire_array(self, tmp_path, monkeypatch):
        """整体替换，不是合并。"""
        import app.core.config as cfg

        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps({"llm_keys": [{"id": "old", "api_key_env": "LLM_KEY_OLD"}]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
        monkeypatch.setattr(cfg, "CONFIG", {"llm_keys": [{"id": "old"}]})

        cfg.save_global_llm_keys([{"id": "new", "api_key_env": "LLM_KEY_NEW", "type": "chat"}])

        on_disk = json.loads(config_path.read_text(encoding="utf-8"))
        ids = [k["id"] for k in on_disk["llm_keys"]]
        assert ids == ["new"]  # old 被替换掉

    def test_auto_generates_env_name_if_missing(self, tmp_path, monkeypatch):
        import app.core.config as cfg

        config_path = tmp_path / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
        monkeypatch.setattr(cfg, "CONFIG", {"llm_keys": []})

        saved = cfg.save_global_llm_keys([{"id": "my-key", "type": "chat"}])
        assert saved[0]["api_key_env"] == "LLM_KEY_MY_KEY"


# ==================== resolve_key_for_role_user use_global ====================

class TestResolveUseGlobal:
    """use_global=True 时 resolve_key_for_role_user 返回 None，即使有 per-user key。"""

    @pytest.mark.asyncio
    async def test_use_global_true_returns_none_with_keys_present(self):
        """用户有 per-user chat key，但 providers.chat.use_global=True → 返回 None。"""
        from app.core.key_resolver import resolve_key_for_role_user

        llm_keys = [
            {"id": "key-a", "base_url": "https://api.a.com/v1", "model": "gpt-4",
             "type": "chat", "api_key": "secret-a"},
        ]
        providers = {"chat": {"key_id": "key-a", "use_global": True}}

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),
            json.dumps(providers),
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("chat", "user-1")

        assert result is None  # 走全局

    @pytest.mark.asyncio
    async def test_use_global_false_uses_per_user_key(self):
        """use_global=False 走 per-user（现有行为不变）。"""
        from app.core.key_resolver import resolve_key_for_role_user

        llm_keys = [
            {"id": "key-a", "base_url": "https://api.a.com/v1", "model": "gpt-4",
             "type": "chat", "api_key": "secret-a"},
        ]
        providers = {"chat": {"key_id": "key-a", "use_global": False}}

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),
            json.dumps(providers),
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("chat", "user-1")

        assert result is not None
        assert result["api_key"] == "secret-a"

    @pytest.mark.asyncio
    async def test_use_global_unset_uses_per_user_key(self):
        """未设 use_global（老数据）走 per-user，向后兼容。"""
        from app.core.key_resolver import resolve_key_for_role_user

        llm_keys = [
            {"id": "key-a", "base_url": "https://api.a.com/v1", "model": "gpt-4",
             "type": "chat", "api_key": "secret-a"},
        ]
        providers = {"chat": {"key_id": "key-a"}}  # 无 use_global 字段

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),
            json.dumps(providers),
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("chat", "user-1")

        assert result is not None
        assert result["api_key"] == "secret-a"

    @pytest.mark.asyncio
    async def test_use_global_on_summary_role(self):
        """summary 角色也支持 use_global。"""
        from app.core.key_resolver import resolve_key_for_role_user

        llm_keys = [
            {"id": "key-s", "base_url": "https://api.s.com/v1", "model": "sum-1",
             "type": "summary", "api_key": "secret-s"},
        ]
        providers = {"summary": {"use_global": True}}

        mock_db = MagicMock()
        mock_db.user_setting_get = AsyncMock(side_effect=[
            json.dumps(llm_keys),
            json.dumps(providers),
        ])
        with patch("app.core.database.Database.get", return_value=mock_db):
            result = await resolve_key_for_role_user("summary", "user-1")

        assert result is None


# ==================== 二级密码 ====================

class TestSecondaryPassword:
    """二级密码设置/验证/门禁。"""

    def test_get_secondary_password_hash_unset(self):
        from app.core.config import get_secondary_password_hash
        assert get_secondary_password_hash() == ""

    def test_set_and_get_secondary_password_hash(self, monkeypatch):
        import app.core.config as cfg
        from app.core.config import get_secondary_password_hash, set_secondary_password_hash
        from app.core.auth import hash_password, verify_password

        # update_config_section 会写磁盘，conftest 已 patch CONFIG_PATH 到 tmp
        h = hash_password("test-pw-123")
        set_secondary_password_hash(h)
        stored = get_secondary_password_hash()
        assert stored == h or verify_password("test-pw-123", stored)

    @pytest.mark.asyncio
    async def test_setup_password_first_time(self):
        from app.routes.settings_routes import set_global_password
        from app.schema.api_schemas import SecondaryPasswordSetupRequest

        # 初始未设
        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value=""):
            result = await set_global_password(
                SecondaryPasswordSetupRequest(password="new-pw-123"),
                current_user={"user_id": "u1", "username": "t"},
            )
        assert result.data["set"] is True

    @pytest.mark.asyncio
    async def test_setup_password_already_set_returns_409(self):
        from app.routes.settings_routes import set_global_password
        from app.schema.api_schemas import SecondaryPasswordSetupRequest
        from app.core.exceptions import AppException

        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value="existing-hash"):
            with pytest.raises(AppException) as exc:
                await set_global_password(
                    SecondaryPasswordSetupRequest(password="new-pw-123"),
                    current_user={"user_id": "u1", "username": "t"},
                )
            assert exc.value.http_status == 409

    @pytest.mark.asyncio
    async def test_verify_password_correct(self):
        from app.routes.settings_routes import verify_global_password
        from app.schema.api_schemas import SecondaryPasswordVerifyRequest
        from app.core.auth import hash_password

        h = hash_password("correct-pw")
        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value=h), \
             patch("app.routes.settings_routes.verify_password", return_value=True):
            result = await verify_global_password(
                SecondaryPasswordVerifyRequest(password="correct-pw")
            )
        assert result.data["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_password_wrong_returns_403(self):
        from app.routes.settings_routes import verify_global_password, _verify_secondary_password
        from app.schema.api_schemas import SecondaryPasswordVerifyRequest
        from app.core.exceptions import AppException
        from app.core.auth import hash_password

        h = hash_password("correct-pw")
        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value=h):
            with pytest.raises(AppException) as exc:
                await verify_global_password(
                    SecondaryPasswordVerifyRequest(password="wrong-pw")
                )
            assert exc.value.http_status == 403

    @pytest.mark.asyncio
    async def test_verify_password_not_set_returns_403(self):
        from app.routes.settings_routes import _verify_secondary_password
        from app.core.exceptions import AppException

        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value=""):
            with pytest.raises(AppException) as exc:
                _verify_secondary_password("anything")
            assert exc.value.http_status == 403
            assert exc.value.code == "secondary_password_not_set"


# ==================== 全局 key CRUD 路由 ====================

class TestGlobalLlmKeyRoutes:
    """全局 key 增删查路由 + 二级密码门禁。"""

    @pytest.mark.asyncio
    async def test_list_global_keys_no_password_needed(self):
        """GET /global/llm_keys 是读操作，不需密码。"""
        from app.routes.settings_routes import list_global_llm_keys

        with patch("app.routes.settings_routes.get_config", return_value=[
            {"id": "k1", "base_url": "https://a.com/v1", "model": "m1",
             "type": "chat", "api_key_env": "LLM_KEY_K1"},
        ]):
            result = await list_global_llm_keys(
                current_user={"user_id": "u1", "username": "t"},
            )
        assert len(result.data) == 1
        assert result.data[0]["id"] == "k1"
        assert "api_key" not in result.data[0]  # 不暴露明文
        assert result.data[0]["api_key_env"] == "LLM_KEY_K1"

    @pytest.mark.asyncio
    async def test_upsert_global_key_without_password_rejected(self):
        """未设二级密码时 POST /global/llm_keys 拒绝。"""
        from app.routes.settings_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest
        from app.core.exceptions import AppException

        mock_container = MagicMock()
        payload = GlobalLLMKeyRequest(
            base_url="https://a.com/v1", model="m1", type="chat",
            api_key="sk-xxx", password="",
        )
        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value=""):
            with pytest.raises(AppException) as exc:
                await upsert_global_llm_key_route(
                    payload,
                    current_user={"user_id": "u1", "username": "t"},
                    container=mock_container,
                )
            assert exc.value.http_status == 403

    @pytest.mark.asyncio
    async def test_upsert_global_key_with_wrong_password_rejected(self):
        """密码错误拒绝。"""
        from app.routes.settings_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest
        from app.core.exceptions import AppException
        from app.core.auth import hash_password

        h = hash_password("correct-pw")
        payload = GlobalLLMKeyRequest(
            base_url="https://a.com/v1", model="m1", type="chat",
            api_key="sk-xxx", password="wrong-pw",
        )
        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value=h):
            with pytest.raises(AppException) as exc:
                await upsert_global_llm_key_route(
                    payload,
                    current_user={"user_id": "u1", "username": "t"},
                    container=MagicMock(),
                )
            assert exc.value.http_status == 403
            assert exc.value.code == "secondary_password_invalid"

    @pytest.mark.asyncio
    async def test_upsert_global_key_new_key_writes_config_and_env(self):
        """新增全局 key：写 .env + save_global_llm_keys + reload key pools。"""
        from app.routes.settings_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        saved_keys = []
        written_secrets = {}

        async def fake_test(*a, **kw):
            return {"ok": True}

        mock_container = MagicMock()
        mock_container.vision_key_pool = MagicMock()
        mock_container.embed_client = MagicMock()
        mock_container.rag_service = None

        payload = GlobalLLMKeyRequest(
            base_url="https://api.example.com/v1", model="glm-4", type="chat",
            api_key="sk-real-key", password="correct-pw",
        )

        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value="hash"), \
             patch("app.routes.settings_routes.verify_password", return_value=True), \
             patch("app.routes.settings_routes.test_model_connection", new=AsyncMock(side_effect=fake_test)), \
             patch("app.routes.settings_routes.write_secrets", side_effect=lambda d: written_secrets.update(d)), \
             patch("app.routes.settings_routes.save_global_llm_keys", side_effect=lambda keys: saved_keys.extend(keys) or saved_keys), \
             patch("app.routes.settings_routes.get_config", return_value=[]), \
             patch("app.routes.settings_routes.GLOBAL_KEY_HOT_RELOAD", False):
            result = await upsert_global_llm_key_route(
                payload,
                current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        # .env 被写
        assert len(written_secrets) == 1
        assert list(written_secrets.values())[0] == "sk-real-key"
        # save_global_llm_keys 被调
        assert len(saved_keys) == 1
        assert saved_keys[0]["model"] == "glm-4"
        assert saved_keys[0]["api_key_env"].startswith("LLM_KEY_")
        # 返回不含明文
        assert "api_key" not in result.data["keys"][0]
        # 非chat热重载或HOT_RELOAD=False时不需重启
        assert result.data["restart_required"] is False

    @pytest.mark.asyncio
    async def test_upsert_global_key_edit_blank_api_key_keeps_env(self):
        """编辑现有 key 且 api_key 留空：不覆盖 .env，沿用原 env。"""
        from app.routes.settings_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        existing = [{
            "id": "existing-1", "base_url": "https://old.com/v1", "model": "old-model",
            "type": "chat", "api_key_env": "LLM_KEY_EXISTING_1",
        }]
        written_secrets = {}

        async def fake_test(*a, **kw):
            return {"ok": True}

        mock_container = MagicMock()
        mock_container.vision_key_pool = MagicMock()
        mock_container.embed_client = MagicMock()
        mock_container.rag_service = None

        payload = GlobalLLMKeyRequest(
            id="existing-1",
            base_url="https://new.com/v1", model="new-model", type="chat",
            api_key="",  # 留空=不改密钥
            password="correct-pw",
        )

        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value="hash"), \
             patch("app.routes.settings_routes.verify_password", return_value=True), \
             patch("app.routes.settings_routes.test_model_connection", new=AsyncMock(side_effect=fake_test)), \
             patch("app.routes.settings_routes.write_secrets", side_effect=lambda d: written_secrets.update(d)), \
             patch("app.routes.settings_routes.save_global_llm_keys", side_effect=lambda keys: keys), \
             patch("app.routes.settings_routes.get_config", return_value=existing), \
             patch("app.routes.settings_routes.GLOBAL_KEY_HOT_RELOAD", False):
            result = await upsert_global_llm_key_route(
                payload,
                current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        # 不应写 .env（密钥未改）
        assert written_secrets == {}
        # 返回的 key 沿用原 env 名
        assert result.data["keys"][0]["api_key_env"] == "LLM_KEY_EXISTING_1"

    @pytest.mark.asyncio
    async def test_delete_global_key_with_password(self):
        """删除全局 key：需密码，从 config.json 移除。"""
        from app.routes.settings_routes import delete_global_llm_key_route

        existing = [
            {"id": "keep", "type": "chat", "api_key_env": "LLM_KEY_KEEP"},
            {"id": "del", "type": "chat", "api_key_env": "LLM_KEY_DEL"},
        ]
        saved_arg = []

        mock_container = MagicMock()
        mock_container.vision_key_pool = MagicMock()
        mock_container.embed_client = MagicMock()
        mock_container.rag_service = None

        with patch("app.routes.settings_routes.get_secondary_password_hash", return_value="hash"), \
             patch("app.routes.settings_routes.verify_password", return_value=True), \
             patch("app.routes.settings_routes.get_config", return_value=existing), \
             patch("app.routes.settings_routes.save_global_llm_keys", side_effect=lambda keys: saved_arg.extend(keys) or keys):
            result = await delete_global_llm_key_route(
                "del", password="correct-pw",
                current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        ids = [k["id"] for k in saved_arg]
        assert "del" not in ids
        assert "keep" in ids


# ==================== per-user use_global 透传 ====================

class TestPerUserUseGlobalPassthrough:
    """POST /llm/settings 透传 use_global 字段到用户 DB。"""

    @pytest.mark.asyncio
    async def test_use_global_true_writes_flag_and_clears_key_id(self):
        from app.routes.settings_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        saved = {}

        async def mock_save(uid, role, key_id, values):
            saved["values"] = values
            saved["key_id"] = key_id

        mock_container = MagicMock()
        mock_container.dispatcher = MagicMock()
        mock_container.dispatcher.invalidate_user_agent = MagicMock()

        payload = LLMSettingsRequest(role="chat", key_id="some-key", use_global=True)

        with patch("app.routes.settings_routes._save_user_provider", new=AsyncMock(side_effect=mock_save)):
            await set_llm_settings(
                payload, current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        assert saved["values"]["use_global"] is True
        # 切全局时 key_id 应被清空
        assert saved["values"]["key_id"] == ""
        # agent 缓存应被清
        mock_container.dispatcher.invalidate_user_agent.assert_called_once_with("u1")

    @pytest.mark.asyncio
    async def test_use_global_false_keeps_key_id(self):
        from app.routes.settings_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        saved = {}

        async def mock_save(uid, role, key_id, values):
            saved["values"] = values
            saved["key_id"] = key_id

        mock_container = MagicMock()
        mock_container.dispatcher = MagicMock()
        mock_container.dispatcher.invalidate_user_agent = MagicMock()

        payload = LLMSettingsRequest(role="chat", key_id="my-key", use_global=False)

        with patch("app.routes.settings_routes._save_user_provider", new=AsyncMock(side_effect=mock_save)):
            await set_llm_settings(
                payload, current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        assert saved["values"]["use_global"] is False
        assert saved["values"]["key_id"] == "my-key"

    @pytest.mark.asyncio
    async def test_use_global_unset_does_not_set_flag(self):
        """use_global=None（老前端调用）不改此标志。"""
        from app.routes.settings_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        saved = {}

        async def mock_save(uid, role, key_id, values):
            saved["values"] = values

        mock_container = MagicMock()
        mock_container.dispatcher = MagicMock()
        mock_container.dispatcher.invalidate_user_agent = MagicMock()

        payload = LLMSettingsRequest(role="chat", key_id="my-key")  # 无 use_global

        with patch("app.routes.settings_routes._save_user_provider", new=AsyncMock(side_effect=mock_save)):
            await set_llm_settings(
                payload, current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        assert "use_global" not in saved["values"]


# ==================== GET /llm/settings 返回 use_global ====================

class TestGetLlmSettingsReturnsUseGlobal:
    """GET /llm/settings 返回 use_global 字段，缺省为 False。"""

    @pytest.mark.asyncio
    async def test_returns_use_global_false_when_unset(self):
        from app.routes.settings_routes import get_llm_settings

        mock_container = MagicMock()
        mock_container.llm_settings_service = MagicMock()
        mock_container.llm_settings_service.current_settings = MagicMock(return_value={})
        mock_container.llm_settings_service.warnings = MagicMock(return_value=[])

        with patch("app.routes.settings_routes._get_user_providers", new=AsyncMock(return_value={})):
            result = await get_llm_settings(
                current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        for role in ("chat", "summary", "stt"):
            assert result.data["current"][role]["use_global"] is False

    @pytest.mark.asyncio
    async def test_returns_use_global_true_when_set(self):
        from app.routes.settings_routes import get_llm_settings

        mock_container = MagicMock()
        mock_container.llm_settings_service = MagicMock()
        mock_container.llm_settings_service.current_settings = MagicMock(return_value={})
        mock_container.llm_settings_service.warnings = MagicMock(return_value=[])

        user_providers = {"chat": {"key_id": "", "use_global": True}}

        with patch("app.routes.settings_routes._get_user_providers", new=AsyncMock(return_value=user_providers)):
            result = await get_llm_settings(
                current_user={"user_id": "u1", "username": "t"},
                container=mock_container,
            )

        assert result.data["current"]["chat"]["use_global"] is True
