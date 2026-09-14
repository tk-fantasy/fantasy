"""Targeted coverage tests for LLM clients, key management, config, auth and misc core modules.

Boundary-mocking only: HTTP transport (httpx shared client), Database, time/sleep,
password hashing. No real network, no real config.json / .env / app/data touched
(conftest auto-patches CONFIG/CONFIG_PATH to tmp).
"""
from __future__ import annotations

import json
import logging
import os
import time
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

import app.core.auth as auth_mod
import app.core.config as cfg_mod
import app.container as container_mod
import app.core.key_healing as healing_mod
import app.core.key_resolver as resolver_mod
import app.core.net_guard as net_guard_mod
import app.core.ws_registry as ws_mod
import app.services.llm_key_service as key_service_mod
import app.services.llm_settings_service as settings_service_mod
import app.services.model_test_service as mts_mod
from app.core.exceptions import AppException, VisionInferenceException


# ---------------------------------------------------------------- helpers
def _make_frame(h: int = 8, w: int = 16) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _fake_http_client(response=None, status: int = 200, body: dict | None = None):
    """httpx.AsyncClient stand-in: .post/.is_closed/.raise_for_status/json."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = json.dumps(body or {})
    resp.json = MagicMock(return_value=body if body is not None else {})
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.is_closed = False
    return client, resp


@pytest.fixture
def patched_shared_client(monkeypatch):
    """Patch the shared httpx client used by vision client + record calls.

    post_chat/post_json 走 llm_base_client 命名空间的 _get_shared_client，
    _post_to_entry_async 走 llm_vision_client 的——两处都指向同一个 fake。
    """
    import app.clients.llm_base_client as base_cli
    import app.clients.llm_vision_client as vc

    client, resp = _fake_http_client(
        body={"choices": [{"message": {"content": " 1 "}}]}
    )
    monkeypatch.setattr(vc, "_get_shared_client", lambda: client)
    monkeypatch.setattr(base_cli, "_get_shared_client", lambda: client)
    # no real backoff sleeps
    monkeypatch.setattr(vc.asyncio, "sleep", AsyncMock())
    return client


def _vision_client() -> "app.clients.llm_vision_client.LlmVisionClient":
    from app.clients.llm_vision_client import LlmVisionClient

    return LlmVisionClient()


# ================================================================ vision
class TestVisionEncodeHelpers:
    def test_encode_frame_b64_returns_base64(self):
        from app.clients.llm_vision_client import encode_frame_b64
        import base64

        b64 = encode_frame_b64(_make_frame(), max_side=64, jpeg_quality=70)
        assert base64.b64decode(b64)[:3] == b"\xff\xd8\xff"  # JPEG magic

    def test_downscale_zero_max_side_returns_original(self):
        from app.clients.llm_vision_client import downscale_for_vision

        frame = _make_frame()
        assert downscale_for_vision(frame, max_side=0) is frame

    def test_downscale_small_frame_returns_original(self):
        from app.clients.llm_vision_client import downscale_for_vision

        frame = _make_frame(8, 16)
        out = downscale_for_vision(frame, max_side=64)
        assert out is frame  # 最长边未超限，不缩放

    def test_downscale_resizes_large_frame(self):
        from app.clients.llm_vision_client import downscale_for_vision

        frame = _make_frame(200, 400)  # h=200, w=400
        out = downscale_for_vision(frame, max_side=100)
        assert max(out.shape[0], out.shape[1]) == 100
        assert out.shape[1] == 100 and out.shape[0] == 50  # 等比

    def test_encode_frame_b64_failure_raises(self, monkeypatch):
        import app.clients.llm_vision_client as vc

        monkeypatch.setattr(vc.cv2, "imencode", lambda *a, **kw: (False, None))
        with pytest.raises(VisionInferenceException, match="图像编码失败"):
            vc.encode_frame_b64(_make_frame(), 64, 70)

    def test_encode_frames_b64_batch(self):
        from app.clients.llm_vision_client import _encode_frames_b64

        out = _encode_frames_b64([_make_frame(), _make_frame(4, 4)], 64, 70)
        assert len(out) == 2
        assert all(isinstance(s, str) and s for s in out)


class TestVisionClientLifecycle:
    def test_init_and_set_key_pool(self):
        from app.clients.llm_vision_client import LlmVisionClient

        client = LlmVisionClient()
        assert client._key_pool is None
        pool = MagicMock()
        client.set_key_pool(pool)
        assert client._key_pool is pool

    def test_load_reads_downscale_config(self, monkeypatch):
        cfg_mod.CONFIG["vision"]["downscale_max_side"] = 224
        cfg_mod.CONFIG["vision"]["jpeg_quality"] = 55
        cfg_mod.CONFIG["llm"]["vision_timeout_seconds"] = 9
        try:
            client = _vision_client()
            assert client._max_side == 224
            assert client._jpeg_quality == 55
            assert client._timeout == 9
        finally:
            cfg_mod.CONFIG["vision"].pop("downscale_max_side", None)
            cfg_mod.CONFIG["vision"].pop("jpeg_quality", None)
            cfg_mod.CONFIG["llm"].pop("vision_timeout_seconds", None)


class TestClassifyFrame:
    async def test_disabled_returns_no_event(self):
        client = _vision_client()
        client._enabled = False
        result = await client.classify_frame(_make_frame())
        assert result == {"enabled": False, "event": "no_event", "feedback": "视觉模型未启用。"}

    async def test_payload_structure_multimodal(self, patched_shared_client):
        client = _vision_client()
        result = await client.classify_frame(_make_frame(), focus="有人吗")
        # classify_frame 原样透传 post_chat 响应（不做 strip）
        assert result["choices"][0]["message"]["content"] == " 1 "
        call = patched_shared_client.post.call_args
        payload = call.kwargs["json"]
        assert payload["model"] == client.model
        assert payload["stream"] is False
        assert payload["max_tokens"] == 128
        assert payload["temperature"] == 0.1
        content = payload["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert "关注: 有人吗" in content[0]["text"]
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    async def test_no_focus_omits_line_and_plain_content_without_multimodal(
        self, patched_shared_client
    ):
        cfg_mod.CONFIG["providers"]["vision"] = {"multimodal": False}
        try:
            client = _vision_client()
            await client.classify_frame(_make_frame())
            payload = patched_shared_client.post.call_args.kwargs["json"]
            content = payload["messages"][0]["content"]
            assert isinstance(content, str)
            assert "关注" not in content
        finally:
            cfg_mod.CONFIG["providers"].pop("vision", None)

    async def test_timeout_override(self, patched_shared_client):
        client = _vision_client()
        await client.classify_frame(_make_frame(), timeout=7)
        assert patched_shared_client.post.call_args.kwargs["timeout"] == 7


class TestAskAboutFrame:
    async def test_disabled_message(self):
        client = _vision_client()
        client._enabled = False
        assert await client.ask_about_frame(_make_frame(), "房间有人吗") == (
            "视觉模型未启用，无法分析画面。"
        )

    async def test_non_multimodal_message(self):
        client = _vision_client()
        client._enabled = True
        client._multimodal_override = None  # not used; patch role cfg below
        cfg_mod.CONFIG["providers"]["vision"] = {"multimodal": False}
        try:
            client.reload()
            msg = await client.ask_about_frame(_make_frame(), "q")
            assert "多模态" in msg
        finally:
            cfg_mod.CONFIG["providers"].pop("vision", None)

    async def test_answer_returned(self, patched_shared_client):
        client = _vision_client()
        answer = await client.ask_about_frame(_make_frame(), "有人在看电视吗")
        assert answer == "1"
        payload = patched_shared_client.post.call_args.kwargs["json"]
        assert payload["max_tokens"] == 256
        assert "有人在看电视吗" in payload["messages"][0]["content"][0]["text"]

    async def test_empty_choices_fallback(self, patched_shared_client):
        patched_shared_client.post.return_value.json.return_value = {"choices": []}
        client = _vision_client()
        assert await client.ask_about_frame(_make_frame(), "q") == "视觉模型没有返回内容。"

    async def test_whitespace_content_fallback(self, patched_shared_client):
        patched_shared_client.post.return_value.json.return_value = {
            "choices": [{"message": {"content": "   "}}]
        }
        client = _vision_client()
        assert await client.ask_about_frame(_make_frame(), "q") == "视觉模型没有返回内容。"


class TestAskAboutFrames:
    async def test_disabled_message(self):
        client = _vision_client()
        client._enabled = False
        assert "未启用" in await client.ask_about_frames([_make_frame()], "q")

    async def test_non_multimodal_message(self):
        cfg_mod.CONFIG["providers"]["vision"] = {"multimodal": False}
        try:
            client = _vision_client()
            client._enabled = True
            msg = await client.ask_about_frames([_make_frame()], "q")
            assert "多模态" in msg
        finally:
            cfg_mod.CONFIG["providers"].pop("vision", None)

    async def test_multi_frame_prompt_contains_count(self, patched_shared_client):
        client = _vision_client()
        answer = await client.ask_about_frames([_make_frame(), _make_frame(), _make_frame()], "他在做什么")
        assert answer == "1"
        payload = patched_shared_client.post.call_args.kwargs["json"]
        assert payload["max_tokens"] == 512
        text = payload["messages"][0]["content"][0]["text"]
        assert "3 张画面" in text
        assert "他在做什么" in text
        assert len(payload["messages"][0]["content"]) == 4  # text + 3 images

    async def test_empty_choices_fallback(self, patched_shared_client):
        patched_shared_client.post.return_value.json.return_value = {}
        client = _vision_client()
        assert await client.ask_about_frames([_make_frame()], "q") == "视觉模型没有返回内容。"


class TestEvaluateCondition:
    async def test_blank_condition_short_circuits(self, patched_shared_client):
        assert await _vision_client().evaluate_condition([_make_frame()], "   ") == "0"
        patched_shared_client.post.assert_not_called()

    async def test_disabled_no_multimodal_or_no_frames_returns_zero(self):
        client = _vision_client()
        client._enabled = False
        assert await client.evaluate_condition([_make_frame()], "有火") == "0"
        client2 = _vision_client()
        cfg_mod.CONFIG["providers"]["vision"] = {"multimodal": False}
        try:
            client2.reload()
            assert await client2.evaluate_condition([_make_frame()], "有火") == "0"
        finally:
            cfg_mod.CONFIG["providers"].pop("vision", None)
        client3 = _vision_client()
        assert await client3.evaluate_condition([], "有火") == "0"

    async def test_pre_encoded_empty_list_returns_zero(self, patched_shared_client):
        assert await _vision_client().evaluate_condition([], "有火", pre_encoded_b64=[]) == "0"
        patched_shared_client.post.assert_not_called()

    async def test_fallback_single_key_path_with_context(self, patched_shared_client):
        client = _vision_client()
        result = await client.evaluate_condition(
            [_make_frame()], "有火", context_info="时间 22:00，室内"
        )
        assert result == "1"  # mocked content " 1 " stripped
        payload = patched_shared_client.post.call_args.kwargs["json"]
        assert payload["max_tokens"] == 16
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert "家庭管家" in messages[0]["content"]
        user_text = messages[1]["content"][0]["text"]
        assert "当前环境信息" in user_text
        assert "condition: 有火" in user_text

    async def test_fallback_without_context_and_empty_choices(self, patched_shared_client):
        patched_shared_client.post.return_value.json.return_value = {"choices": []}
        result = await _vision_client().evaluate_condition([_make_frame()], "有火")
        assert result == "0"

    def _pool(self, acquire_return):
        pool = MagicMock()
        pool.available = True
        pool.acquire = AsyncMock(return_value=acquire_return)
        pool.release = AsyncMock()
        return pool

    async def test_pool_path_success_releases_with_ok_true(self, patched_shared_client):
        client = _vision_client()
        entry = {
            "id": "k1", "base_url": "http://node:11434/v1", "chat_path": "/chat/completions",
            "model": "qwen-vl", "api_key": "sk-abc",
        }
        client.set_key_pool(self._pool(entry))
        result = await client.evaluate_condition([_make_frame()], "有火")
        assert result == "1"
        call = patched_shared_client.post.call_args
        assert call.args[0] == "http://node:11434/v1/chat/completions"
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk-abc"
        client._key_pool.release.assert_awaited_once_with(entry, success=True)

    async def test_pool_busy_returns_zero(self, patched_shared_client):
        client = _vision_client()
        pool = self._pool(None)
        client.set_key_pool(pool)
        assert await client.evaluate_condition([_make_frame()], "有火") == "0"
        patched_shared_client.post.assert_not_called()
        pool.release.assert_not_awaited()

    async def test_pool_failure_releases_with_ok_false(self, patched_shared_client):
        import httpx

        patched_shared_client.post.side_effect = httpx.ConnectError("boom")
        client = _vision_client()
        entry = {"id": "k1", "base_url": "http://x/v1", "chat_path": "/c", "model": "m", "api_key": ""}
        client.set_key_pool(self._pool(entry))
        assert await client.evaluate_condition([_make_frame()], "有火") == "0"
        client._key_pool.release.assert_awaited_once_with(entry, success=False)


class TestPostToEntryAsync:
    def _entry(self, api_key: str = "sk-1"):
        return {
            "id": "k1", "base_url": "http://node:11434/v1",
            "chat_path": "/chat/completions", "model": "vl", "api_key": api_key,
        }

    async def test_success_with_auth_header(self, patched_shared_client):
        from app.clients.llm_vision_client import LlmVisionClient

        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(), [{"type": "text", "text": "c"}], timeout=5,
            system_prompt="SYS",
        )
        assert (content, ok) == ("1", True)
        payload = patched_shared_client.post.call_args.kwargs["json"]
        assert payload["model"] == "vl"
        assert payload["messages"][0] == {"role": "system", "content": "SYS"}
        headers = patched_shared_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-1"

    async def test_no_api_key_no_header_and_no_choices(self, patched_shared_client):
        from app.clients.llm_vision_client import LlmVisionClient

        patched_shared_client.post.return_value.json.return_value = {"choices": []}
        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(api_key=""), [{"type": "text", "text": "c"}], timeout=5,
        )
        assert (content, ok) == ("0", True)
        assert "Authorization" not in patched_shared_client.post.call_args.kwargs["headers"]

    async def test_429_then_success_retries(self, patched_shared_client):
        from app.clients.llm_vision_client import LlmVisionClient

        patched_shared_client.post.side_effect = [
            MagicMock(status_code=429, raise_for_status=MagicMock()),
            MagicMock(
                status_code=200, raise_for_status=MagicMock(),
                json=MagicMock(return_value={"choices": [{"message": {"content": "0"}}]}),
            ),
        ]
        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(), [{"type": "text", "text": "c"}], timeout=5,
        )
        assert (content, ok) == ("0", True)
        assert patched_shared_client.post.await_count == 2

    async def test_connect_error_exhausts_retries(self, patched_shared_client):
        import httpx

        from app.clients.llm_vision_client import LlmVisionClient

        patched_shared_client.post.side_effect = httpx.ConnectError("down")
        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(), [{"type": "text", "text": "c"}], timeout=5,
        )
        assert (content, ok) == ("0", False)
        assert patched_shared_client.post.await_count == 3  # max_retries=2

    async def test_timeout_exception_returns_failure(self, patched_shared_client):
        import httpx

        from app.clients.llm_vision_client import LlmVisionClient

        patched_shared_client.post.side_effect = httpx.TimeoutException("t/o")
        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(), [{"type": "text", "text": "c"}], timeout=5, max_retries=1,
        )
        assert (content, ok) == ("0", False)

    async def test_http_status_error_no_retry(self, patched_shared_client):
        import httpx

        from app.clients.llm_vision_client import LlmVisionClient

        resp = MagicMock(status_code=500)
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        patched_shared_client.post.return_value = resp
        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(), [{"type": "text", "text": "c"}], timeout=5,
        )
        assert (content, ok) == ("0", False)
        assert patched_shared_client.post.await_count == 1

    async def test_invalid_json_returns_failure(self, patched_shared_client):
        from app.clients.llm_vision_client import LlmVisionClient

        patched_shared_client.post.return_value.json.side_effect = ValueError("bad json")
        content, ok = await LlmVisionClient._post_to_entry_async(
            self._entry(), [{"type": "text", "text": "c"}], timeout=5,
        )
        assert (content, ok) == ("0", False)


# ================================================================ api_key_manager
@pytest.fixture
def key_config(monkeypatch):
    """Two vision keys in CONFIG with concurrency 1 for deterministic rotation."""
    keys = [
        {"id": "k1", "type": "vision", "base_url": "http://a/v1", "model": "m1",
         "api_key": "sk-1", "chat_path": "/chat/completions"},
        {"id": "k2", "type": "vision", "base_url": "http://b/v1", "model": "m2",
         "api_key": "sk-2", "chat_path": "/chat/completions"},
    ]
    cfg_mod.CONFIG["llm_keys"] = keys
    cfg_mod.CONFIG["providers"]["vision"] = {"max_concurrency": 1}
    yield keys
    cfg_mod.CONFIG["llm_keys"] = []
    cfg_mod.CONFIG["providers"].pop("vision", None)


class TestApiKeyManager:
    def _mgr(self):
        from app.services.api_key_manager import ApiKeyManager

        return ApiKeyManager(role="vision")

    def test_load_sync_builds_entries(self, key_config):
        mgr = self._mgr()
        assert mgr.total_concurrency_sync == 2  # 2 keys x concurrency 1
        assert mgr.available is True
        assert [e["id"] for e in mgr._entries] == ["k1", "k2"]
        assert all(e["in_use"] == 0 and e["fail_count"] == 0 for e in mgr._entries)

    def test_available_false_when_no_keys(self):
        from app.services.api_key_manager import ApiKeyManager

        assert ApiKeyManager(role="vision").available is False

    def test_reload_preserves_runtime_state_by_id(self, key_config):
        mgr = self._mgr()
        # 运行时状态挂在 k2（reload 后保留的 key）上
        mgr._entries[1]["in_use"] = 1
        mgr._entries[1]["fail_count"] = 2
        mgr._entries[1]["cooldown_until"] = 123.0
        mgr._cursor = 1

        # k1 下线（状态丢弃），k2 保留（状态按 id 合并），k3 上线
        cfg_mod.CONFIG["llm_keys"] = [
            {"id": "k2", "type": "vision", "base_url": "http://b/v1", "model": "m2", "api_key": "sk-2"},
            {"id": "k3", "type": "vision", "base_url": "http://c/v1", "model": "m3", "api_key": "sk-3"},
        ]
        mgr.reload()
        by_id = {e["id"]: e for e in mgr._entries}
        assert set(by_id) == {"k2", "k3"}
        assert by_id["k2"]["in_use"] == 1 and by_id["k2"]["fail_count"] == 2
        assert by_id["k2"]["cooldown_until"] == 123.0
        assert by_id["k3"]["in_use"] == 0  # 新 key 不继承状态
        assert mgr._cursor == 0

    async def test_acquire_rotates_and_release_restores(self, key_config):
        mgr = self._mgr()
        e1 = await mgr.acquire(timeout=0.1)
        assert e1["id"] == "k1" and e1["in_use"] == 1
        e2 = await mgr.acquire(timeout=0.05)  # k1 占满，k2 轮转命中
        assert e2["id"] == "k2"
        e3 = await mgr.acquire(timeout=0.05)  # 全占满 → 超时 None
        assert e3 is None
        await mgr.release(e1)  # 无 success 参数：只归还名额
        e4 = await mgr.acquire(timeout=0.05)
        assert e4["id"] == "k1"

    async def test_acquire_empty_pool_returns_none(self):
        mgr = self._mgr().__class__(role="vision")  # no keys in this CONFIG
        assert await mgr.acquire(timeout=0.01) is None

    async def test_release_success_resets_fail_count(self, key_config):
        mgr = self._mgr()
        e = await mgr.acquire()
        e["fail_count"] = 2
        await mgr.release(e, success=True)
        assert e["fail_count"] == 0 and e["in_use"] == 0

    async def test_release_failure_trips_circuit_after_threshold(self, key_config):
        mgr = self._mgr()
        e1 = await mgr.acquire()
        await mgr.release(e1, success=False)
        await mgr.release(e1, success=False)
        assert e1["cooldown_until"] == 0.0  # 2 次未达阈值
        await mgr.release(e1, success=False)
        assert e1["fail_count"] == 3
        assert e1["cooldown_until"] > time.time()
        # 冷却期内 acquire 跳过 k1，但 k2 仍可用
        got = await mgr.acquire(timeout=0.05)
        assert got["id"] == "k2"

    async def test_acquire_deadline_already_passed_returns_none(self, key_config):
        mgr = self._mgr()
        # 占满全部并发名额（concurrency=1 x2）
        e1 = await mgr.acquire(timeout=0.05)
        e2 = await mgr.acquire(timeout=0.05)
        assert e1 is not None and e2 is not None
        # 负超时 → deadline 已过期，跳过等待直接走 remaining<=0 分支
        assert await mgr.acquire(timeout=-1.0) is None

    async def test_release_never_goes_negative(self, key_config):
        mgr = self._mgr()
        e = mgr._entries[0]
        await mgr.release(e)  # in_use 已经是 0
        assert e["in_use"] == 0

    async def test_circuit_recovers_after_cooldown(self, key_config, monkeypatch):
        from app.services import api_key_manager as akm

        mgr = self._mgr()
        # 两个 key 各失败 3 次 → 全部熔断
        for _ in range(2):
            e = await mgr.acquire(timeout=0.05)
            for _ in range(3):
                await mgr.release(e, success=False)
        assert await mgr.acquire(timeout=0.02) is None  # 全部熔断 → None
        # 时间快进越过冷却期
        real_time = akm.time
        monkeypatch.setattr(
            akm, "time",
            MagicMock(wraps=real_time, **{"time.return_value": real_time.time() + 61}),
        )
        got = await mgr.acquire(timeout=0.05)
        assert got is not None


# ================================================================ config
class TestParseDotenv:
    def test_parses_quotes_comments_and_ignores_junk(self):
        from app.core.config import _parse_dotenv

        text = (
            "# comment\n"
            "\n"
            "A=1\n"
            "B = spaced \n"
            'C="quoted"\n'
            "D='sq'\n"
            "no_equals_line\n"
            "=novalue-key\n"
            "E=a=b\n"
        )
        assert _parse_dotenv(text) == {
            "A": "1", "B": "spaced", "C": "quoted", "D": "sq", "E": "a=b",
        }


class TestLoadDotenv:
    def test_missing_env_file_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / "nope.env")
        cfg_mod._load_dotenv()  # 不抛异常即通过

    def test_fills_only_missing_env_vars(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('FILL_ME="from file"\nKEEP_ME=from-env\n', encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "ENV_PATH", env_file)
        monkeypatch.setenv("KEEP_ME", "from-env")
        monkeypatch.setenv("FILL_ME", "")  # 空值 → 允许补位
        cfg_mod._load_dotenv()
        assert os.environ["FILL_ME"] == "from file"
        assert os.environ["KEEP_ME"] == "from-env"


class TestWriteSecrets:
    def test_merges_and_syncs_environ(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OLD=1\n", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "ENV_PATH", env_file)
        try:
            cfg_mod.write_secrets({"NEW_KEY": "v2"})
        finally:
            os.environ.pop("NEW_KEY", None)
        content = env_file.read_text(encoding="utf-8")
        assert "OLD=1" in content and "NEW_KEY=v2" in content
        assert content.startswith("#")


class TestLoadFileConfig:
    def test_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "nope.json")
        assert cfg_mod._load_file_config() == {}

    def test_invalid_json_without_backup_returns_empty(self, monkeypatch, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{broken", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", p)
        assert cfg_mod._load_file_config() == {}

    def test_invalid_json_recovers_from_backup(self, monkeypatch, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{broken", encoding="utf-8")
        bak = tmp_path / "config.json.bak"
        bak.write_text('{"llm": {"enabled": false}}', encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", p)
        assert cfg_mod._load_file_config() == {"llm": {"enabled": False}}

    def test_invalid_json_and_backup_both_bad(self, monkeypatch, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{broken", encoding="utf-8")
        (tmp_path / "config.json.bak").write_text("also bad", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", p)
        assert cfg_mod._load_file_config() == {}


class TestDeepMerge:
    def test_nested_merge_and_scalar_override(self):
        from app.core.config import _deep_merge

        base = {"a": {"x": 1, "y": 2}, "b": 1}
        out = _deep_merge(base, {"a": {"y": 3, "z": 4}, "b": 9})
        assert out == {"a": {"x": 1, "y": 3, "z": 4}, "b": 9}
        # dict 覆盖非 dict 值时直接替换
        out2 = _deep_merge({"a": 1}, {"a": {"x": 2}})
        assert out2 == {"a": {"x": 2}}


class TestLoadEnvOverride:
    def test_all_env_vars_mapped(self, monkeypatch):
        monkeypatch.setenv("LLM_ENABLED", "1")
        monkeypatch.setenv("LLM_BASE_URL", "http://env:8080/v1")
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("HA_URL", "http://ha:8123")
        monkeypatch.setenv("HA_TOKEN", "tok")
        ov = cfg_mod._load_env_override()
        assert ov["llm"]["enabled"] is True
        assert ov["llm"]["base_url"] == "http://env:8080/v1"
        assert ov["llm"]["chat_model"] == "env-model"
        assert ov["ha"] == {"url": "http://ha:8123", "token": "tok"}
        # 死键的 env 覆盖已删：LOG_LEVEL 由 main 直接读 os.getenv；
        # LLM_EMBED_MODEL 不再映射（embed_model 键已清理）

    def test_absent_env_vars_yield_empty_sections(self, monkeypatch):
        for name in ("LLM_ENABLED", "LLM_BASE_URL", "LLM_MODEL", "LLM_EMBED_MODEL",
                     "LOG_LEVEL", "HA_URL", "HA_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        ov = cfg_mod._load_env_override()
        assert ov == {"llm": {}, "ha": {}}


class TestSafeBackup:
    def test_backup_created(self, monkeypatch, tmp_path):
        p = tmp_path / "config.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", p)
        cfg_mod._safe_backup_config()
        assert (tmp_path / "config.json.bak").read_text(encoding="utf-8") == '{"a": 1}'

    def test_missing_file_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "nope.json")
        cfg_mod._safe_backup_config()
        assert not (tmp_path / "config.json.bak").exists()

    def test_oserror_swallowed_with_warning(self, monkeypatch, tmp_path, caplog):
        p = tmp_path / "config.json"
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", p)
        monkeypatch.setattr(
            type(p), "read_text",
            MagicMock(side_effect=OSError("disk full")),
        )
        with caplog.at_level(logging.WARNING, logger="app.core.config"):
            cfg_mod._safe_backup_config()  # 不抛
        assert any("Failed to backup" in r.message for r in caplog.records)


class TestUpdateConfigSection:
    def test_writes_disk_merges_memory_and_strips_llm_keys_plaintext(
        self, monkeypatch, tmp_path
    ):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"llm_keys": [{"id": "k1", "api_key": "PLAIN"}]}), encoding="utf-8"
        )
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        monkeypatch.setenv("HA_URL", "http://ha-from-env:8123")
        out = cfg_mod.update_config_section("llm", {"enabled": False, "chat_model": "new"})
        assert out["chat_model"] == "new"
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["llm_keys"][0] == {"id": "k1"}  # 明文 api_key 被剥离
        assert "config.json.bak" in {f.name for f in tmp_path.iterdir()}
        # 内存 CONFIG 合并 + 环境变量覆盖重新生效
        assert cfg_mod.CONFIG["llm"]["chat_model"] == "new"
        assert cfg_mod.CONFIG["ha"]["url"] == "http://ha-from-env:8123"

    def test_nested_section_merge(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"providers": {"chat": {"key_id": "old"}}}), encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        cfg_mod.update_config_section("providers", {"chat": {"max_concurrency": 3}})
        # 磁盘上是深层合并结果（旧 key_id 保留）
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["providers"]["chat"] == {"key_id": "old", "max_concurrency": 3}
        # 内存合并了新值
        assert cfg_mod.CONFIG["providers"]["chat"]["max_concurrency"] == 3


class TestUpdateMemoryConfig:
    def test_top_level_and_nested(self):
        cfg_mod.update_memory_config("llm_keys", [{"id": "x"}])
        assert cfg_mod.get_config("llm_keys") == [{"id": "x"}]
        cfg_mod.update_memory_config("providers.chat.key_id", "k9")
        assert cfg_mod.get_config("providers.chat.key_id") == "k9"

    def test_intermediate_non_dict_is_replaced(self):
        cfg_mod.update_memory_config("deep.a.b", 1)
        assert cfg_mod.get_config("deep.a.b") == 1
        cfg_mod.update_memory_config("deep.a", "scalar")
        cfg_mod.update_memory_config("deep.a.b", 2)  # scalar 被替换为 {}
        assert cfg_mod.get_config("deep.a.b") == 2


class TestLlmKeyCrud:
    def test_upsert_requires_id(self):
        with pytest.raises(ValueError, match="id"):
            cfg_mod.upsert_llm_key({"type": "chat"})

    def test_upsert_new_key_autogenerates_env_and_writes_secret(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "ENV_PATH", env_file)
        try:
            keys = cfg_mod.upsert_llm_key(
                {"id": "my-prov", "type": "chat", "base_url": "u"}, api_key_value=" sk-live "
            )
        finally:
            os.environ.pop("LLM_KEY_MY_PROV", None)
        entry = keys[-1]
        assert entry["api_key_env"] == "LLM_KEY_MY_PROV"
        assert "LLM_KEY_MY_PROV=sk-live" in env_file.read_text(encoding="utf-8")

    def test_upsert_replaces_by_id(self):
        cfg_mod.update_memory_config("llm_keys", [{"id": "k1", "type": "chat"}])
        keys = cfg_mod.upsert_llm_key({"id": "k1", "type": "chat", "model": "m2"})
        assert len(keys) == 1 and keys[0]["model"] == "m2"
        keys = cfg_mod.upsert_llm_key({"id": "k2", "type": "embed"})
        assert [k["id"] for k in keys] == ["k1", "k2"]

    def test_delete_llm_key(self):
        cfg_mod.update_memory_config(
            "llm_keys", [{"id": "k1"}, {"id": "k2"}]
        )
        keys = cfg_mod.delete_llm_key("k1")
        assert [k["id"] for k in keys] == ["k2"]
        assert cfg_mod.delete_llm_key("ghost") == keys  # 删不存在的返回原列表


class TestSaveGlobalLlmKeys:
    def test_sanitizes_and_persists(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        out = cfg_mod.save_global_llm_keys([
            {"id": "prov-1", "type": "chat", "api_key": "PLAINTEXT", "base_url": "u"},
            {"id": "prov-2", "type": "embed"},  # 无 env 名 → 自动生成
        ])
        assert all("api_key" not in k for k in out)
        assert out[0]["api_key_env"] == "LLM_KEY_PROV_1"
        assert out[1]["api_key_env"] == "LLM_KEY_PROV_2"
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert len(on_disk["llm_keys"]) == 2
        assert cfg_mod.CONFIG["llm_keys"] is out


class TestSecondaryPassword:
    def test_get_and_set_roundtrip(self, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        assert cfg_mod.get_secondary_password_hash() == ""
        cfg_mod.set_secondary_password_hash("abc123")
        assert cfg_mod.get_secondary_password_hash() == "abc123"
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["security"]["secondary_password_hash"] == "abc123"


# ================================================================ auth
@pytest.fixture
def fake_auth_paths(monkeypatch, tmp_path):
    """Redirect auth.Path so .env / .jwt_secret resolution lands in tmp.

    anchor = tmp/a/b 充当 auth.py 的文件路径：_load_env_minimal 推导
    tmp/.env；_resolve_jwt_secret 推导密钥文件到 tmp/data/.jwt_secret。
    """
    anchor = tmp_path / "a" / "b"
    monkeypatch.setattr(auth_mod, "Path", lambda _p: anchor)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    return tmp_path


class TestJwtSecretResolution:
    def test_env_var_wins(self, fake_auth_paths, monkeypatch):
        monkeypatch.setenv("JWT_SECRET", "env-secret")
        assert auth_mod._resolve_jwt_secret() == "env-secret"

    def test_persisted_file_reused(self, fake_auth_paths):
        secret_file = fake_auth_paths / "data" / ".jwt_secret"
        secret_file.parent.mkdir(parents=True)
        secret_file.write_text("file-secret\n", encoding="utf-8")
        assert auth_mod._resolve_jwt_secret() == "file-secret"

    def test_generates_and_persists_new_secret(self, fake_auth_paths):
        secret = auth_mod._resolve_jwt_secret()
        secret_file = fake_auth_paths / "data" / ".jwt_secret"
        assert len(secret) == 64
        assert secret_file.read_text(encoding="utf-8") == secret

    def test_oserror_falls_back_to_ephemeral_secret(self, monkeypatch, tmp_path):
        # 密钥文件的父目录 "data" 已被文件占用 → mkdir 抛 OSError
        (tmp_path / "data").write_text("i am a file", encoding="utf-8")
        monkeypatch.setattr(auth_mod, "Path", lambda _p: tmp_path / "a" / "b")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        secret = auth_mod._resolve_jwt_secret()
        assert len(secret) == 64  # 随机密钥，未持久化


class TestLoadEnvMinimal:
    def test_missing_file_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(auth_mod, "Path", lambda _p: tmp_path / "no.env")
        auth_mod._load_env_minimal()  # 不抛

    def test_setdefault_does_not_override_existing(self, monkeypatch, tmp_path):
        # 真 .env 位置 = anchor.resolve().parent.parent.parent / ".env" = tmp/.env
        (tmp_path / ".env").write_text(
            "AUTH_NEW_VAR=x\nAUTH_EXISTING_VAR=file-val\n", encoding="utf-8"
        )
        monkeypatch.setattr(auth_mod, "Path", lambda _p: tmp_path / "l1" / "l2" / "l3")
        monkeypatch.setenv("AUTH_EXISTING_VAR", "env-val")
        auth_mod._load_env_minimal()
        try:
            assert os.environ["AUTH_NEW_VAR"] == "x"
            assert os.environ["AUTH_EXISTING_VAR"] == "env-val"
        finally:
            os.environ.pop("AUTH_NEW_VAR", None)


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        hashed = auth_mod.hash_password("s3cret-pass")
        assert hashed != "s3cret-pass"
        assert auth_mod.verify_password("s3cret-pass", hashed) is True
        assert auth_mod.verify_password("wrong", hashed) is False


class TestRevocation:
    def test_revoke_without_jti_is_noop(self):
        auth_mod.revoke_token({})  # 不抛，黑名单不变

    def test_revoke_then_verify_rejected(self):
        token = auth_mod.create_access_token("u1", "bob")
        payload = auth_mod.verify_token(token)
        auth_mod.revoke_token(payload)
        assert auth_mod.is_revoked(payload["jti"]) is True
        with pytest.raises(AppException) as ei:
            auth_mod.verify_token(token)
        assert ei.value.code == "token_revoked"

    def test_revoke_cleans_expired_entries(self, monkeypatch):
        auth_mod.revoke_token({"jti": "old", "exp": int(time.time()) - 10})
        auth_mod.revoke_token({"jti": "fresh", "exp": int(time.time()) + 600})
        assert auth_mod.is_revoked("old") is False
        assert auth_mod.is_revoked("fresh") is True

    def test_is_revoked_none_jti(self):
        assert auth_mod.is_revoked(None) is False
        assert auth_mod.is_revoked("") is False


class TestVerifyTokenErrors:
    def test_expired_token(self):
        import jwt as pyjwt

        past = {"sub": "u", "exp": int(time.time()) - 100, "jti": "j"}
        token = pyjwt.encode(past, auth_mod.JWT_SECRET, algorithm="HS256")
        with pytest.raises(AppException) as ei:
            auth_mod.verify_token(token)
        assert ei.value.code == "token_expired"

    def test_garbage_token(self):
        with pytest.raises(AppException) as ei:
            auth_mod.verify_token("not-a-jwt")
        assert ei.value.code == "invalid_token"


class TestCookiesAndRequestExtraction:
    def _resp(self):
        from fastapi import Response

        return Response()

    def test_set_and_clear_auth_cookies(self):
        from fastapi import Response

        resp = Response()
        auth_mod.set_auth_cookies(resp, "acc", "ref", secure=True)
        set_cookies = resp.headers.getlist("set-cookie")
        assert any("aether_token=acc" in h and "Secure" in h for h in set_cookies)
        assert any("aether_refresh_token=ref" in h for h in set_cookies)
        auth_mod.clear_auth_cookies(resp)
        cleared = resp.headers.getlist("set-cookie")
        assert any('aether_token=""' in h for h in cleared)
        assert any('aether_refresh_token=""' in h for h in cleared)

    def test_is_secure_request_forwarded_proto(self):
        req_https = MagicMock()
        req_https.headers.get.return_value = "https"
        assert auth_mod.is_secure_request(req_https) is True
        req_http = MagicMock()
        req_http.headers.get.return_value = "http, https"  # 多级代理取第一跳
        assert auth_mod.is_secure_request(req_http) is False

    def test_is_secure_request_falls_back_to_scheme(self):
        req = MagicMock()
        req.headers.get.return_value = ""  # 无 X-Forwarded-Proto
        req.url.scheme = "https"
        assert auth_mod.is_secure_request(req) is True
        req.url.scheme = "http"
        assert auth_mod.is_secure_request(req) is False

    def test_extract_token_prefers_header_then_cookie(self):
        req = MagicMock()
        req.headers = {"Authorization": "Bearer hdr-token"}
        req.cookies = {"aether_token": "cookie-token"}
        assert auth_mod.extract_token_from_request(req) == "hdr-token"
        req.headers = {"Authorization": "Basic xxx"}
        assert auth_mod.extract_token_from_request(req) == "cookie-token"
        req.cookies = {}
        assert auth_mod.extract_token_from_request(req) is None

    def test_extract_refresh_token_from_cookie(self):
        req = MagicMock()
        req.cookies = {"aether_refresh_token": "r-tok"}
        assert auth_mod.extract_refresh_token_from_request(req) == "r-tok"


class TestGetCurrentUser:
    async def test_missing_token_rejected(self):
        req = MagicMock()
        req.headers = {}
        req.cookies = {}
        with pytest.raises(AppException) as ei:
            await auth_mod.get_current_user(req)
        assert ei.value.code == "missing_auth"

    async def test_refresh_token_type_rejected_for_api(self):
        refresh = auth_mod.create_refresh_token("u1")
        req = MagicMock()
        req.headers = {"Authorization": f"Bearer {refresh}"}
        with pytest.raises(AppException) as ei:
            await auth_mod.get_current_user(req)
        assert ei.value.code == "invalid_token_type"

    async def test_valid_access_token_returns_user(self):
        token = auth_mod.create_access_token("u1", "alice")
        req = MagicMock()
        req.headers = {"Authorization": f"Bearer {token}"}
        user = await auth_mod.get_current_user(req)
        assert user == {"user_id": "u1", "username": "alice"}


class TestGetCurrentAdmin:
    def _db(self, user):
        db = MagicMock()
        db.user_get_by_id = AsyncMock(return_value=user)
        return db

    async def test_non_admin_rejected(self):
        with patch("app.core.database.Database.get", return_value=self._db({"is_admin": 0})):
            with pytest.raises(AppException) as ei:
                await auth_mod.get_current_admin({"user_id": "u2", "username": "kid"})
            assert ei.value.code == "admin_required"

    async def test_db_failure_rejected(self):
        db = MagicMock()
        db.user_get_by_id = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.core.database.Database.get", return_value=db):
            with pytest.raises(AppException) as ei:
                await auth_mod.get_current_admin({"user_id": "ghost", "username": ""})
            assert ei.value.code == "admin_required"

    async def test_admin_passes(self):
        with patch(
            "app.core.database.Database.get",
            return_value=self._db({"is_admin": 1, "username": "owner"}),
        ):
            result = await auth_mod.get_current_admin({"user_id": "u1", "username": "owner"})
        assert result["is_admin"] == 1


# ================================================================ key_resolver
class TestFindKeyById:
    def test_found_and_not_found(self):
        cfg_mod.update_memory_config("llm_keys", [{"id": "a", "type": "chat"}])
        assert resolver_mod.find_key_by_id("a") == {"id": "a", "type": "chat"}
        assert resolver_mod.find_key_by_id("ghost") is None

    def test_empty_id_returns_none(self):
        assert resolver_mod.find_key_by_id(None) is None
        assert resolver_mod.find_key_by_id("") is None


class TestAutoSelectKey:
    def test_selects_first_matching_with_key(self, monkeypatch):
        cfg_mod.update_memory_config("llm_keys", [
            {"id": "e", "type": "embed", "api_key": ""},
            {"id": "c", "type": "chat", "api_key_env": "TEST_AUTOS_KEY"},
            {"id": "c2", "type": "chat", "api_key": "direct"},
        ])
        monkeypatch.setenv("TEST_AUTOS_KEY", "env-key")
        assert resolver_mod.auto_select_key("chat")["id"] == "c"

    def test_no_match_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_AUTOS_KEY", raising=False)
        cfg_mod.update_memory_config("llm_keys", [{"id": "c", "type": "chat", "api_key_env": "TEST_AUTOS_KEY"}])
        assert resolver_mod.auto_select_key("chat") is None
        assert resolver_mod.auto_select_key("vision") is None


class TestGetKeysForRole:
    def test_builds_normalized_entries(self, monkeypatch):
        cfg_mod.update_memory_config("llm_keys", [
            {"id": "v1", "type": "vision", "base_url": "http://x/v1/",
             "model": "vl", "api_key_env": "TEST_GKFR_KEY", "embed_path": "/v1/embeddings"},
            {"id": "v2", "type": "vision", "api_key": ""},  # 无 key → 跳过
            {"id": "c1", "type": "chat", "api_key": "sk"},  # 角色不符 → 跳过
        ])
        monkeypatch.setenv("TEST_GKFR_KEY", "sk-env")
        entries = resolver_mod.get_keys_for_role("vision")
        assert len(entries) == 1
        e = entries[0]
        assert e == {
            "id": "v1", "api_key": "sk-env", "model": "vl",
            "base_url": "http://x/v1", "chat_path": "/chat/completions",
            "embed_path": "/v1/embeddings",
        }


class TestResolveApiKey:
    def test_env_preferred_over_direct(self, monkeypatch):
        monkeypatch.setenv("TEST_RAK_KEY", "from-env")
        assert resolver_mod.resolve_api_key({"api_key_env": "TEST_RAK_KEY", "api_key": "direct"}) == "from-env"
        assert resolver_mod.resolve_api_key({"api_key": "direct"}) == "direct"
        assert resolver_mod.resolve_api_key({}) == ""


class TestResolveKeyForRole:
    def test_binding_wins_over_auto_select(self, monkeypatch):
        cfg_mod.update_memory_config("llm_keys", [
            {"id": "first", "type": "chat", "api_key": "auto"},
            {"id": "bound", "type": "chat", "api_key": "bound-key", "base_url": "http://b/v1/"},
        ])
        cfg_mod.update_memory_config("providers.chat.key_id", "bound")
        k = resolver_mod.resolve_key_for_role("chat")
        assert k["id"] == "bound" and k["api_key"] == "bound-key"
        assert k["base_url"] == "http://b/v1/"  # 原样返回（rstrip 仅 get_keys_for_role 做）

    def test_auto_select_when_binding_missing(self, monkeypatch):
        cfg_mod.update_memory_config("llm_keys", [{"id": "first", "type": "chat", "api_key": "auto"}])
        cfg_mod.update_memory_config("providers.chat.key_id", "ghost")
        k = resolver_mod.resolve_key_for_role("chat")
        assert k["id"] == "first"

    def test_no_keys_returns_none(self):
        cfg_mod.update_memory_config("llm_keys", [])
        assert resolver_mod.resolve_key_for_role("chat") is None


class TestResolveKeyForRoleUser:
    def _db(self, llm_keys, providers=None):
        db = MagicMock()
        db.user_setting_get = AsyncMock(side_effect=[
            llm_keys, json.dumps(providers) if providers is not None else None,
        ])
        return db

    async def test_empty_user_id_returns_none(self):
        assert await resolver_mod.resolve_key_for_role_user("chat", "") is None

    async def test_missing_llm_keys_setting_returns_none(self):
        with patch("app.core.database.Database.get", return_value=self._db(None)):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None

    async def test_binding_matched_but_no_key_anywhere_returns_none(self, monkeypatch):
        # key_id 绑定命中（绑定路径不校验 key 存在），但既无明文也无 env → None
        monkeypatch.delenv("TEST_USERKEY_ENV2", raising=False)
        db = self._db(
            json.dumps([{"id": "k", "type": "chat", "base_url": "u"}]),
            {"chat": {"key_id": "k"}},
        )
        with patch("app.core.database.Database.get", return_value=db):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None

    async def test_invalid_llm_keys_json_returns_none(self):
        with patch("app.core.database.Database.get", return_value=self._db("{broken")):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None

    async def test_empty_key_list_returns_none(self):
        with patch("app.core.database.Database.get", return_value=self._db(json.dumps([]))):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None

    async def test_invalid_providers_json_treated_as_empty(self):
        db = MagicMock()
        db.user_setting_get = AsyncMock(side_effect=[
            json.dumps([{"id": "k", "type": "chat", "api_key": "sk", "base_url": "u"}]),
            "{broken",
        ])
        with patch("app.core.database.Database.get", return_value=db):
            k = await resolver_mod.resolve_key_for_role_user("chat", "u1")
        assert k["api_key"] == "sk"

    async def test_use_global_flag_returns_none(self):
        db = self._db(
            json.dumps([{"id": "k", "type": "chat", "api_key": "sk"}]),
            {"chat": {"use_global": True}},
        )
        with patch("app.core.database.Database.get", return_value=db):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None

    async def test_no_key_entry_after_binding_and_autoselect_returns_none(self):
        db = self._db(
            json.dumps([{"id": "k", "type": "embed", "api_key": "sk"}]),  # 类型不匹配
            {"chat": {"key_id": "ghost"}},
        )
        with patch("app.core.database.Database.get", return_value=db):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None

    async def test_env_fallback_for_plaintext(self, monkeypatch):
        monkeypatch.setenv("TEST_USERKEY_ENV", "sk-from-env")
        db = self._db(json.dumps([
            {"id": "k", "type": "chat", "base_url": "u", "api_key_env": "TEST_USERKEY_ENV"},
        ]))
        with patch("app.core.database.Database.get", return_value=db):
            k = await resolver_mod.resolve_key_for_role_user("chat", "u1")
        assert k["api_key"] == "sk-from-env"

    async def test_no_key_anywhere_returns_none(self, monkeypatch):
        monkeypatch.delenv("TEST_USERKEY_ENV2", raising=False)
        db = self._db(json.dumps([{"id": "k", "type": "chat", "base_url": "u"}]))
        with patch("app.core.database.Database.get", return_value=db):
            assert await resolver_mod.resolve_key_for_role_user("chat", "u1") is None


# ================================================================ key_healing
def _heal_db(users_and_keys):
    """users_and_keys: list[(user_dict, llm_keys_json_or_None)]"""
    db = MagicMock()
    db.user_list_all = AsyncMock(return_value=[u for u, _ in users_and_keys])
    db.user_setting_get = AsyncMock(side_effect=[k for _, k in users_and_keys])
    return db


class TestHealEdgeBranches:
    def test_is_api_key_valid_blank_variants(self):
        assert healing_mod.is_api_key_valid("") is False
        assert healing_mod.is_api_key_valid("   ") is False

    async def test_no_global_keys_returns_empty(self):
        with patch("app.core.config.get_config", return_value=[]):
            assert await healing_mod.heal_global_keys_from_user_db() == {}

    async def test_all_roles_valid_returns_empty_without_touching_db(self):
        global_keys = [
            {"id": "k1", "type": "chat", "api_key": "sk-ok"},
            {"id": "k2", "type": "embed", "api_key_env": "TEST_ALLVALID"},
        ]
        db = MagicMock()
        db.user_list_all = AsyncMock()
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.key_resolver.resolve_api_key", side_effect=lambda k: k.get("api_key") or "env"), \
             patch("app.core.database.Database.get", return_value=db):
            assert await healing_mod.heal_global_keys_from_user_db() == {}
        db.user_list_all.assert_not_awaited()

    async def test_no_valid_key_in_user_db_returns_empty(self):
        # 用户 key 全是占位符 → 扫描完也没有可恢复的 → {}
        global_keys = [{"id": "k", "type": "embed", "api_key": ""}]
        users = [({"id": "u1"}, json.dumps([
            {"id": "k", "type": "embed", "api_key": "your_siliconflow_api_key_here"},
        ]))]
        db = _heal_db(users)
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config") as mock_mem, \
             patch("app.core.config.write_secrets") as mock_env, \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=db):
            assert await healing_mod.heal_global_keys_from_user_db() == {}
        mock_mem.assert_not_called()
        mock_env.assert_not_called()

    async def test_write_back_skips_roles_without_healed_key(self):
        # chat 全局有效（不在 healed 集合）→ 写回循环 continue；embed 被恢复
        global_keys = [
            {"id": "kc", "type": "chat", "api_key": "sk-chat-ok"},
            {"id": "ke", "type": "embed", "api_key": ""},
        ]
        users = [({"id": "u1"}, json.dumps([
            {"id": "ke", "type": "embed", "api_key": "sk-embed-fixed"},
        ]))]
        db = _heal_db(users)
        mem = {}
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config", side_effect=lambda k, v: mem.update({k: v})), \
             patch("app.core.key_resolver.resolve_api_key",
                   side_effect=lambda k: k.get("api_key") or ""), \
             patch("app.core.database.Database.get", return_value=db):
            healed = await healing_mod.heal_global_keys_from_user_db()
        assert healed == {"embed": "sk-embed-fixed"}
        chat_entry = next(k for k in mem["llm_keys"] if k["type"] == "chat")
        embed_entry = next(k for k in mem["llm_keys"] if k["type"] == "embed")
        assert "api_key" not in chat_entry or chat_entry["api_key"] == "sk-chat-ok"
        assert embed_entry["api_key"] == "sk-embed-fixed"

    async def test_second_entry_same_role_skipped_once_healed(self):
        # 同一用户第二个同角色条目已被 healed → continue 跳过
        global_keys = [{"id": "k", "type": "embed", "api_key": ""}]
        users = [({"id": "u1"}, json.dumps([
            {"id": "k", "type": "embed", "api_key": "sk-first"},
            {"id": "k2", "type": "embed", "api_key": "sk-second"},
        ]))]
        db = _heal_db(users)
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=db):
            healed = await healing_mod.heal_global_keys_from_user_db()
        assert healed == {"embed": "sk-first"}

    async def test_breaks_after_all_roles_healed(self):
        global_keys = [{"id": "k", "type": "embed", "api_key": ""}]
        users = [
            ({"id": "u1"}, json.dumps([{"id": "k", "type": "embed", "api_key": "sk-fixed"}])),
            ({"id": "u2"}, json.dumps([{"id": "k", "type": "embed", "api_key": "sk-never-read"}])),
        ]
        db = _heal_db(users)
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=db):
            healed = await healing_mod.heal_global_keys_from_user_db()
        assert healed == {"embed": "sk-fixed"}
        assert db.user_setting_get.await_count == 1  # u2 未被扫描

    async def test_user_without_llm_keys_is_skipped(self):
        global_keys = [{"id": "k", "type": "embed", "api_key": ""}]
        users = [
            ({"id": "u1"}, None),
            ({"id": "u2"}, json.dumps([{"id": "k", "type": "embed", "api_key": "sk-2"}])),
        ]
        db = _heal_db(users)
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=db):
            healed = await healing_mod.heal_global_keys_from_user_db()
        assert healed == {"embed": "sk-2"}

    async def test_corrupt_user_keys_json_is_skipped(self):
        global_keys = [{"id": "k", "type": "embed", "api_key": ""}]
        users = [
            ({"id": "u1"}, "{corrupt"),
            ({"id": "u2"}, json.dumps([{"id": "k", "type": "embed", "api_key": "sk-2"}])),
        ]
        db = _heal_db(users)
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=db):
            healed = await healing_mod.heal_global_keys_from_user_db()
        assert healed == {"embed": "sk-2"}

    async def test_env_fallback_when_user_key_has_no_plaintext(self, monkeypatch):
        monkeypatch.setenv("TEST_HEAL_ENV", "sk-from-env")
        global_keys = [{"id": "k", "type": "embed", "api_key": "", "api_key_env": "TEST_HEAL_ENV"}]
        # per-user 条目无明文 api_key，带 api_key_env → 回退读环境变量
        users = [({"id": "u1"}, json.dumps([
            {"id": "k", "type": "embed", "api_key_env": "TEST_HEAL_ENV"},
        ]))]
        db = _heal_db(users)
        mem, env_written = {}, {}
        with patch("app.core.config.get_config", return_value=global_keys), \
             patch("app.core.config.update_memory_config", side_effect=lambda k, v: mem.update({k: v})), \
             patch("app.core.config.write_secrets", side_effect=lambda e: env_written.update(e)), \
             patch("app.core.key_resolver.resolve_api_key", return_value=""), \
             patch("app.core.database.Database.get", return_value=db):
            healed = await healing_mod.heal_global_keys_from_user_db()
        assert healed == {"embed": "sk-from-env"}
        assert env_written == {"TEST_HEAL_ENV": "sk-from-env"}
        assert mem["llm_keys"][0]["api_key"] == "sk-from-env"
        assert os.environ["TEST_HEAL_ENV"] == "sk-from-env"


# ================================================================ llm_key_service
def _container(rag=True):
    c = MagicMock()
    c.rag_service = MagicMock() if rag else None
    return c


class TestReloadKeyPools:
    def test_reloads_pools_and_embed_and_rag(self):
        container = _container(rag=True)
        key_service_mod.reload_key_pools(container)
        container.vision_key_pool.reload.assert_called_once()
        container.embed_client.reload.assert_called_once()
        container.rag_service.maybe_rebuild_if_model_changed.assert_called_once()

    def test_rag_absent_is_skipped(self):
        container = _container(rag=False)
        key_service_mod.reload_key_pools(container)  # 不抛


class TestSyncLlmKeysToUser:
    async def test_fills_plaintext_from_env_and_persists(self, monkeypatch):
        monkeypatch.setenv("TEST_SYNC_ENV", "sk-sync")
        cfg_mod.update_memory_config("llm_keys", [
            {"id": "k1", "api_key_env": "TEST_SYNC_ENV"},
            {"id": "k2", "api_key": "already-plain"},
        ])
        db = MagicMock()
        db.user_setting_set = AsyncMock()
        with patch("app.core.database.Database.get", return_value=db):
            await key_service_mod.sync_llm_keys_to_current_user({"user_id": "u1"})
        args = db.user_setting_set.await_args.args
        assert args[0] == "u1" and args[1] == "llm_keys"
        stored = json.loads(args[2])
        assert stored[0]["api_key"] == "sk-sync"
        assert stored[1]["api_key"] == "already-plain"

    async def test_db_failure_is_swallowed(self):
        db = MagicMock()
        db.user_setting_set = AsyncMock(side_effect=RuntimeError("locked"))
        with patch("app.core.database.Database.get", return_value=db):
            await key_service_mod.sync_llm_keys_to_current_user({"user_id": "u1"})  # 不抛


class TestUserProviders:
    async def test_save_merges_into_existing(self):
        db = MagicMock()
        db.user_setting_get = AsyncMock(return_value=json.dumps({"embed": {"key_id": "old"}}))
        db.user_setting_set = AsyncMock()
        with patch("app.core.database.Database.get", return_value=db):
            await key_service_mod.save_user_provider("u1", "chat", "k9", {"model": "m"})
        saved = json.loads(db.user_setting_set.await_args.args[2])
        assert saved["embed"]["key_id"] == "old"
        assert saved["chat"] == {"model": "m", "key_id": "k9"}

    async def test_save_creates_when_absent(self):
        db = MagicMock()
        db.user_setting_get = AsyncMock(return_value=None)
        db.user_setting_set = AsyncMock()
        with patch("app.core.database.Database.get", return_value=db):
            await key_service_mod.save_user_provider("u1", "vision", "k1", {})
        assert json.loads(db.user_setting_set.await_args.args[2])["vision"]["key_id"] == "k1"

    async def test_save_db_failure_is_swallowed(self):
        db = MagicMock()
        db.user_setting_get = AsyncMock(side_effect=RuntimeError("down"))
        with patch("app.core.database.Database.get", return_value=db):
            await key_service_mod.save_user_provider("u1", "chat", "k", {})  # 不抛

    async def test_get_returns_parsed(self):
        db = MagicMock()
        db.user_setting_get = AsyncMock(return_value=json.dumps({"chat": {"key_id": "k"}}))
        with patch("app.core.database.Database.get", return_value=db):
            assert await key_service_mod.get_user_providers("u1") == {"chat": {"key_id": "k"}}

    async def test_get_failure_returns_empty_dict(self):
        db = MagicMock()
        db.user_setting_get = AsyncMock(side_effect=RuntimeError("down"))
        with patch("app.core.database.Database.get", return_value=db):
            assert await key_service_mod.get_user_providers("u1") == {}


class TestGenerateKeyId:
    def test_prefix_from_host_with_random_suffix(self):
        kid = key_service_mod.generate_key_id("https://api.siliconflow.cn/v1")
        prefix, suffix = kid.split("-")
        assert prefix == "api"
        assert len(suffix) == 6 and all(c in "0123456789abcdef" for c in suffix)

    def test_unparseable_url_uses_unknown(self):
        assert key_service_mod.generate_key_id("::::").startswith("unknown-")


class TestVerifySecondaryPassword:
    def test_not_set_rejects(self, monkeypatch):
        monkeypatch.setattr(key_service_mod, "get_secondary_password_hash", lambda: "")
        with pytest.raises(AppException) as ei:
            key_service_mod.verify_secondary_password("whatever")
        assert ei.value.code == "secondary_password_not_set"

    def test_wrong_password_rejected(self, monkeypatch):
        monkeypatch.setattr(key_service_mod, "get_secondary_password_hash", lambda: "h")
        monkeypatch.setattr(key_service_mod, "verify_password", lambda p, h: False)
        with pytest.raises(AppException) as ei:
            key_service_mod.verify_secondary_password("wrong")
        assert ei.value.code == "secondary_password_invalid"

    def test_empty_password_rejected(self, monkeypatch):
        monkeypatch.setattr(key_service_mod, "get_secondary_password_hash", lambda: "h")
        with pytest.raises(AppException):
            key_service_mod.verify_secondary_password("")

    def test_correct_password_passes(self, monkeypatch):
        monkeypatch.setattr(key_service_mod, "get_secondary_password_hash", lambda: "h")
        monkeypatch.setattr(key_service_mod, "verify_password", lambda p, h: True)
        key_service_mod.verify_secondary_password("good")  # 不抛


class TestMaskAndFlag:
    def test_is_secondary_password_set(self, monkeypatch):
        monkeypatch.setattr(key_service_mod, "get_secondary_password_hash", lambda: "x")
        assert key_service_mod.is_secondary_password_set() is True
        monkeypatch.setattr(key_service_mod, "get_secondary_password_hash", lambda: "")
        assert key_service_mod.is_secondary_password_set() is False

    def test_mask_hides_plaintext_and_reports_env_state(self, monkeypatch):
        monkeypatch.setenv("TEST_MASK_SET", "v")
        monkeypatch.delenv("TEST_MASK_UNSET", raising=False)
        masked = key_service_mod.mask_global_keys([
            {"id": "k1", "base_url": "u", "model": "m", "type": "chat",
             "api_key": "PLAIN", "api_key_env": "TEST_MASK_SET"},
            {"id": "k2", "type": "embed", "api_key_env": "TEST_MASK_UNSET"},
            {"id": "k3", "type": "vision"},  # 无 env 名
        ])
        assert masked[0]["api_key_set"] is True and "api_key" not in masked[0]
        assert masked[1]["api_key_set"] is False
        assert masked[2]["api_key_set"] is False
        assert masked[0]["chat_path"] == "/chat/completions"
        assert masked[0]["embed_path"] == "/v1/embeddings"


# ================================================================ llm_settings_service
@pytest.fixture
def svc():
    return settings_service_mod.LlmSettingsService()


class TestLlmSettingsService:
    def test_hooks_run_and_exceptions_swallowed(self, svc, caplog):
        seen = []
        svc.register_reload_hook(lambda: seen.append(1))

        def boom():
            raise RuntimeError("hook broke")

        svc.register_reload_hook(boom)
        with caplog.at_level(logging.ERROR, logger="app.services.llm_settings_service"):
            svc._run_hooks()
        assert seen == [1]
        assert any("reload hook failed" in r.message for r in caplog.records)

    def test_current_settings_defaults(self, svc):
        cfg_mod.CONFIG["providers"] = {}
        s = svc.current_settings()
        assert set(s) == {"chat", "vision", "embed", "stt"}
        assert s["chat"] == {
            "key_id": None, "max_concurrency": 8, "thinking": False, "multimodal": False,
        }

    def test_apply_invalid_role_raises(self, svc):
        with pytest.raises(AppException):
            svc.apply("nope", "k1")

    def test_apply_chat_role_with_hooks(self, svc, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        hooked = []
        svc.register_reload_hook(lambda: hooked.append(True))
        out = svc.apply("chat", "k1", max_concurrency=0, thinking=True)
        assert out["applied"]["max_concurrency"] == 1  # 下限 1
        assert out["applied"]["thinking"] is True
        assert out["applied"]["enabled"] is True
        assert "multimodal" not in out["applied"]  # 仅 vision 有
        assert hooked == [True]
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["providers"]["chat"]["key_id"] == "k1"

    def test_apply_vision_multimodal_defaults_true_when_none(self, svc, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        out = svc.apply("vision", "k1")
        assert out["applied"]["multimodal"] is True
        assert out["applied"]["thinking"] is False

    def test_apply_vision_multimodal_explicit_false(self, svc, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        out = svc.apply("vision", "k1", multimodal=False, thinking=None)
        assert out["applied"]["multimodal"] is False

    def test_apply_embed_has_no_thinking_or_multimodal(self, svc, monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_path)
        out = svc.apply("embed", "k1", thinking=True, multimodal=True)
        assert "thinking" not in out["applied"]
        assert "multimodal" not in out["applied"]

    def test_warnings_all_branches(self, svc, monkeypatch):
        cfg_mod.CONFIG["providers"] = {
            "chat": {},  # 未选 key
            "vision": {"key_id": "ghost"},  # key 不存在
            "embed": {"key_id": "e1", "api_key_env": "TEST_WARN_MISSING"},  # env 未设置
            "stt": {"key_id": "s1"},  # 无 env 无明文
        }
        cfg_mod.CONFIG["llm_keys"] = [
            {"id": "e1", "type": "embed", "api_key_env": "TEST_WARN_MISSING"},
            {"id": "s1", "type": "stt"},
        ]
        monkeypatch.delenv("TEST_WARN_MISSING", raising=False)
        notes = svc.warnings()
        assert any(notes[0].startswith("chat 未选择 key") for _ in [0]) and "未选择 key" in notes[0]
        assert "不存在" in notes[1]
        assert "TEST_WARN_MISSING" in notes[2]
        assert "未设置 API key" in notes[3]
        assert len(notes) == 4


# ================================================================ model_test_service
class TestSilenceWav:
    def test_generates_valid_mono_wav(self):
        data = mts_mod._silence_wav(duration_ms=50, sample_rate=8000)
        import io

        with wave.open(io.BytesIO(data)) as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 8000
            assert wf.getnframes() == 400  # 8000 * 0.05


@pytest.fixture
def fake_new_client(monkeypatch):
    """Patch model_test_service.new_client with an async-CM returning a mock client."""
    holder = {}

    def _install(resp):
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=client)
        cm.__aexit__ = AsyncMock(return_value=False)
        factory = MagicMock(return_value=cm)
        monkeypatch.setattr(mts_mod, "new_client", factory)
        holder["client"] = client
        holder["factory"] = factory
        return client

    return _install


class TestModelConnection:
    async def test_scheme_whitelist_blocks_file(self, fake_new_client):
        result = await mts_mod.test_model_connection("file:///etc/passwd", "m", "chat")
        assert result["ok"] is False
        assert "只允许" in result["error"]
        assert "http" in result["error"] and "file" in result["error"]

    async def test_chat_success_sends_expected_payload(self, fake_new_client):
        resp = MagicMock(status_code=200)
        client = fake_new_client(resp)
        result = await mts_mod.test_model_connection(
            "http://127.0.0.1:11434/v1", "qwen", "chat", api_key="sk-t"
        )
        assert result == {"ok": True}
        url, kwargs = client.post.await_args.args[0], client.post.await_args.kwargs
        assert url == "http://127.0.0.1:11434/v1/chat/completions"
        assert kwargs["headers"]["Authorization"] == "Bearer sk-t"
        assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["json"]["max_tokens"] == 1

    async def test_embed_uses_embed_path_and_input(self, fake_new_client):
        resp = MagicMock(status_code=200)
        client = fake_new_client(resp)
        result = await mts_mod.test_model_connection(
            "http://h/v1", "emb", "embed", chat_path="/chat/completions",
            embed_path="/v1/embeddings",
        )
        assert result["ok"] is True
        kwargs = client.post.await_args.kwargs
        assert kwargs["json"] == {"model": "emb", "input": "test"}

    async def test_stt_sends_multipart_wav(self, fake_new_client):
        resp = MagicMock(status_code=200)
        client = fake_new_client(resp)
        result = await mts_mod.test_model_connection(
            "http://h/", "stt-m", "stt", api_key="sk-s"
        )
        assert result["ok"] is True
        kwargs = client.post.await_args.kwargs
        assert client.post.await_args.args[0] == "http://h/audio/transcriptions"
        filename, fileobj, mime = kwargs["files"]["file"]
        assert filename == "test.wav" and mime == "audio/wav"
        assert fileobj[:4] == b"RIFF"
        assert kwargs["data"] == {"model": "stt-m"}
        assert kwargs["headers"] == {"Authorization": "Bearer sk-s"}

    async def test_http_error_status_reported(self, fake_new_client):
        resp = MagicMock(status_code=401, text="Unauthorized body")
        fake_new_client(resp)
        result = await mts_mod.test_model_connection("http://h", "m", "chat")
        assert result["ok"] is False
        assert result["error"].startswith("HTTP 401:")
        assert "Unauthorized body" in result["error"]

    async def test_timeout_error(self, fake_new_client):
        import httpx

        client = fake_new_client(MagicMock())
        client.post.side_effect = httpx.TimeoutException("t")
        result = await mts_mod.test_model_connection("http://h", "m", "chat", timeout=3.5)
        assert result == {"ok": False, "error": "连接超时（3.5秒）"}

    async def test_connect_error(self, fake_new_client):
        import httpx

        client = fake_new_client(MagicMock())
        client.post.side_effect = httpx.ConnectError("refused")
        result = await mts_mod.test_model_connection("http://h", "m", "chat")
        assert result["ok"] is False
        assert result["error"].startswith("连接失败:")

    async def test_unexpected_error(self, fake_new_client):
        client = fake_new_client(MagicMock())
        client.post.side_effect = RuntimeError("weird")
        result = await mts_mod.test_model_connection("http://h", "m", "chat")
        assert result["ok"] is False
        assert result["error"].startswith("未知错误: weird")


# ================================================================ net_guard
class TestNetGuard:
    def test_invalid_url_returns_format_error(self):
        assert net_guard_mod.url_scheme_error("http://[::1", net_guard_mod.HTTP_SCHEMES) == "URL 格式无效"

    def test_scheme_accepts_and_rejects(self):
        assert net_guard_mod.url_scheme_error("https://a.com", net_guard_mod.HTTP_SCHEMES) is None
        err = net_guard_mod.url_scheme_error("ftp://a.com", net_guard_mod.HTTP_SCHEMES)
        assert err is not None and "ftp" in err
        err_empty = net_guard_mod.url_scheme_error("not a url", net_guard_mod.HTTP_SCHEMES)
        assert "空" in err_empty

    def test_stream_schemes_include_rtsp(self):
        assert net_guard_mod.url_scheme_error("rtsp://cam/1", net_guard_mod.STREAM_SCHEMES) is None
        assert net_guard_mod.url_scheme_error("file://cam", net_guard_mod.STREAM_SCHEMES) is not None

    def test_is_lan_ipv4(self):
        assert net_guard_mod.is_lan_ipv4("192.168.1.50") is True
        assert net_guard_mod.is_lan_ipv4("127.0.0.1") is True
        assert net_guard_mod.is_lan_ipv4("169.254.1.1") is True  # link-local
        assert net_guard_mod.is_lan_ipv4(" 10.0.0.2 ") is True  # strip
        assert net_guard_mod.is_lan_ipv4("8.8.8.8") is False
        assert net_guard_mod.is_lan_ipv4("::1") is False  # IPv6
        assert net_guard_mod.is_lan_ipv4("camera.local") is False
        assert net_guard_mod.is_lan_ipv4("") is False


# ================================================================ ws_registry
@pytest.fixture(autouse=True)
def clean_ws_registry(monkeypatch):
    monkeypatch.setattr(ws_mod, "_sockets", {})
    yield


class TestWsRegistry:
    def test_register_unregister_and_cleanup(self):
        ws1, ws2 = object(), object()
        ws_mod.register("u1", ws1)
        ws_mod.register("u1", ws2)
        assert ws_mod._sockets["u1"] == {ws1, ws2}
        ws_mod.unregister("u1", ws1)
        assert ws_mod._sockets["u1"] == {ws2}
        ws_mod.unregister("u1", ws2)
        assert "u1" not in ws_mod._sockets  # 空集合自动清理

    def test_unregister_unknown_user_noop(self):
        ws_mod.unregister("ghost", object())  # 不抛

    async def test_push_to_user_all_sockets(self):
        ws1, ws2 = MagicMock(), MagicMock()
        ws1.send_json = AsyncMock()
        ws2.send_json = AsyncMock()
        ws_mod.register("u1", ws1)
        ws_mod.register("u1", ws2)
        await ws_mod.push_to_user("u1", {"msg": "hi"})
        ws1.send_json.assert_awaited_once_with({"msg": "hi"})
        ws2.send_json.assert_awaited_once_with({"msg": "hi"})

    async def test_push_failure_isolated(self, caplog):
        good, bad = MagicMock(), MagicMock()
        good.send_json = AsyncMock()
        bad.send_json = AsyncMock(side_effect=RuntimeError("closed"))
        ws_mod.register("u1", bad)
        ws_mod.register("u1", good)
        with caplog.at_level(logging.DEBUG, logger="app.core.ws_registry"):
            await ws_mod.push_to_user("u1", {"m": 1})  # 不抛
        good.send_json.assert_awaited_once()

    async def test_push_to_unknown_user_noop(self):
        await ws_mod.push_to_user("nobody", {"m": 1})  # 不抛

    async def test_push_to_all_broadcasts_every_user(self):
        ws_a, ws_b = MagicMock(), MagicMock()
        ws_a.send_json = AsyncMock()
        ws_b.send_json = AsyncMock()
        ws_mod.register("a", ws_a)
        ws_mod.register("b", ws_b)
        await ws_mod.push_to_all({"alert": True})
        ws_a.send_json.assert_awaited_once_with({"alert": True})
        ws_b.send_json.assert_awaited_once_with({"alert": True})

    async def test_push_to_all_empty_registry_noop(self):
        await ws_mod.push_to_all({"alert": True})  # 不抛


# ================================================================ container
def _services_dict():
    s = {
        "ha_service": MagicMock(name="ha_service"),
        "llm_chat_client": MagicMock(name="llm_chat_client"),
        "vision_client": MagicMock(name="vision_client"),
        "embed_client": MagicMock(name="embed_client"),
        "session_store": MagicMock(),
        "vision_service": MagicMock(),
        "vision_key_pool": MagicMock(),
        "rule_service": MagicMock(),
        "rule_registry_service": MagicMock(),
        "automation_service": MagicMock(),
        "summarization_service": MagicMock(),
        "llm_settings_service": MagicMock(),
        "emoji_service": MagicMock(),
        "mcp_client_manager": MagicMock(),
        "tool_executor": MagicMock(),
        "sg_service": MagicMock(),
        "ha_client_ref": [MagicMock(name="ha_client")],
        "automation_agent_ref": [None],
        "ha_catalog_cache_ref": [""],
    }
    return s


class TestContainer:
    def test_get_container_before_init_raises(self, monkeypatch):
        monkeypatch.setattr(container_mod, "_container", None)
        with pytest.raises(RuntimeError, match="not initialized"):
            container_mod.get_container()

    def test_init_container_and_get_container(self, monkeypatch):
        monkeypatch.setattr(container_mod, "_container", None)
        services = _services_dict()
        metrics = MagicMock(name="metrics")
        c = container_mod.init_container(services, metrics)
        assert container_mod.get_container() is c
        assert c.metrics_service is metrics
        assert c.ha_client is services["ha_client_ref"][0]  # 动态读取 ref[0]
        assert c.camera_manager is None  # 缺省段默认 None
        assert c.ha_controls_cache_ref == [""]  # 新建缓存，不复用入参
        assert c.dispatcher is None

    def test_optional_sections_read_from_services(self, monkeypatch):
        monkeypatch.setattr(container_mod, "_container", None)
        services = _services_dict()
        services["camera_manager"] = MagicMock(name="cam_mgr")
        services["integration_layer"] = MagicMock(name="layer")
        c = container_mod.init_container(services, MagicMock())
        assert c.camera_manager is services["camera_manager"]
        assert c.integration_layer is services["integration_layer"]

    def test_reload_all_clients_with_and_without_rag(self, monkeypatch):
        monkeypatch.setattr(container_mod, "_container", None)
        c = container_mod.init_container(_services_dict(), MagicMock())
        # rag_service 在 lifespan 阶段才赋值，这里手动挂一个 mock 验证联动
        c.rag_service = MagicMock()
        c.reload_all_clients()
        c.llm_chat_client.reload.assert_called_once()
        c.vision_client.reload.assert_called_once()
        c.embed_client.reload.assert_called_once()
        c.rag_service.maybe_rebuild_if_model_changed.assert_called_once()

        c.rag_service = None
        c.reload_all_clients()  # rag 为 None 时不抛
        assert c.embed_client.reload.call_count == 2
