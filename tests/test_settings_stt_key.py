"""Tests for stt key 走 /llm_keys 写入路由（type=stt）。

STT 已纳入 llm_keys 体系：chat_path/embed_path 置空。
b521941 起，stt 新增时也测连接（与 chat/summary/vision/embed 一致）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AppException
from app.schema.api_schemas import LLMKeyRequest


def _mock_container() -> MagicMock:
    """构造一个 mock AppContainer（_reload_key_pools 会访问 vision_key_pool.reload）。"""
    c = MagicMock()
    c.vision_key_pool.reload = MagicMock()
    return c


class TestUpsertSttKeyRoute:
    @pytest.mark.asyncio
    async def test_stt_key_new_runs_test_and_sets_empty_paths(self):
        """新增 type=stt 的 key：也测连接（b521941）、chat_path/embed_path 置空。"""
        from app.routes.settings_routes import upsert_llm_key_route

        captured: dict = {}

        def fake_upsert(entry, api_key_value=None):
            captured["entry"] = entry
            captured["api_key_value"] = api_key_value
            return [entry]

        current_user = {"user_id": "u1"}
        container = _mock_container()
        payload = LLMKeyRequest(
            base_url="https://api.siliconflow.cn/v1",
            model="FunAudioLLM/SenseVoiceSmall",
            type="stt",
            api_key="sk-test",
            id="",  # 新增
        )

        with patch("app.routes.settings_routes.test_model_connection", new_callable=AsyncMock) as mock_test, \
             patch("app.routes.settings_routes.upsert_llm_key", side_effect=fake_upsert), \
             patch("app.routes.settings_routes._sync_llm_keys_to_current_user", new_callable=AsyncMock) as mock_sync:
            mock_test.return_value = {"ok": True}
            result = await upsert_llm_key_route(payload=payload, current_user=current_user, container=container)

        # 新增 stt key 也触发连接测试（b521941 行为）
        mock_test.assert_called_once()
        # 同步到用户设置被调用
        mock_sync.assert_called_once_with(current_user)
        # 写入的 entry path 置空
        entry = captured["entry"]
        assert entry["type"] == "stt"
        assert entry["chat_path"] == ""
        assert entry["embed_path"] == ""
        # 自动生成 id（非空）
        assert entry["id"]
        # api_key_value 透传给 upsert_llm_key
        assert captured["api_key_value"] == "sk-test"
        # 返回 ok 且 data 为 keys 列表
        assert result.code == "ok"
        assert isinstance(result.data, list)

    @pytest.mark.asyncio
    async def test_stt_key_new_test_failure_rejected(self):
        """新增 stt key 时连接测试失败应被拒（b521941：stt 也走测试+拦截）。"""
        from app.routes.settings_routes import upsert_llm_key_route

        payload = LLMKeyRequest(
            base_url="https://api.siliconflow.cn/v1",
            model="FunAudioLLM/SenseVoiceSmall",
            type="stt",
            api_key="sk-bad",
            id="",
        )

        with patch("app.routes.settings_routes.test_model_connection", new_callable=AsyncMock) as mock_test, \
             patch("app.routes.settings_routes.upsert_llm_key") as mock_upsert, \
             patch("app.routes.settings_routes._sync_llm_keys_to_current_user", new_callable=AsyncMock):
            mock_test.return_value = {"ok": False, "error": "401 Unauthorized"}
            with pytest.raises(AppException) as exc_info:
                await upsert_llm_key_route(
                    payload=payload,
                    current_user={"user_id": "u1"},
                    container=_mock_container(),
                )
        assert exc_info.value.http_status == 400
        assert "连接测试失败" in exc_info.value.message
        # 测试失败不应写入
        mock_upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_type_rejected(self):
        """非法 type 抛 AppException(400)。"""
        from app.routes.settings_routes import upsert_llm_key_route

        payload = LLMKeyRequest(
            base_url="https://api.example.com/v1",
            model="m",
            type="foo",
            api_key="sk-test",
            id="",
        )
        with pytest.raises(AppException) as exc_info:
            await upsert_llm_key_route(
                payload=payload,
                current_user={"user_id": "u1"},
                container=_mock_container(),
            )
        assert exc_info.value.http_status == 400
        assert "stt" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_non_stt_new_key_runs_test(self):
        """新增 type=chat 的 key 仍触发连接测试（验证 stt 跳过是针对性的）。"""
        from app.routes.settings_routes import upsert_llm_key_route

        payload = LLMKeyRequest(
            base_url="https://api.example.com/v1",
            model="gpt-test",
            type="chat",
            api_key="sk-test",
            id="",
        )

        with patch("app.routes.settings_routes.test_model_connection", new_callable=AsyncMock) as mock_test, \
             patch("app.routes.settings_routes.upsert_llm_key", return_value=[]), \
             patch("app.routes.settings_routes._sync_llm_keys_to_current_user", new_callable=AsyncMock):
            mock_test.return_value = {"ok": True}
            await upsert_llm_key_route(
                payload=payload,
                current_user={"user_id": "u1"},
                container=_mock_container(),
            )

        mock_test.assert_called_once()
