"""在线更新通道测试 — /api/ops/update/* 的服务层。

覆盖：
- 更新源地址校验与持久化（非法 scheme 拒绝、空值允许=未配置）
- check_update 四态：not_configured / up_to_date / available / incompatible + 拉取失败 error
- 渠道清单解析：非法 JSON / 缺字段拒绝；pack 相对路径与绝对 URL 解析
- download_and_apply：整包 sha256 校验（通过/不一致拒绝）→ 复用 apply_upgrade
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.exceptions import AppException
from app.ops import update_channel as uc


def _client_factory(handler):
    """把模块里新建的 AsyncClient 换成 MockTransport 版（网络隔离）。

    注意：uc.httpx 即全局 httpx 模块，patch 它的 AsyncClient 会全局生效，
    工厂内必须先捕获真实类再调用，否则无限递归。
    """
    real_client = httpx.AsyncClient
    real_transport = httpx.MockTransport

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=real_transport(handler))
    return factory


def _channel_payload(version="1.1.0", min_compatible="1.0.0", **extra):
    return {
        "version": version,
        "min_compatible": min_compatible,
        "notes": "测试版本",
        "pack": "aether-update-1.1.0.tar.gz",
        "size_bytes": 1024,
        **extra,
    }


# ==================== 设置与校验 ====================

class TestManifestUrl:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(AppException) as ei:
            uc.set_manifest_url("ftp://example.com/x.json")
        assert ei.value.http_status == 400

    def test_rejects_garbage(self):
        with pytest.raises(AppException):
            uc.set_manifest_url("not a url")

    def test_empty_allowed_and_persists(self):
        saved = {}
        with patch.object(uc, "update_config_section", side_effect=lambda s, v: saved.update(v)):
            assert uc.set_manifest_url("") == ""
            assert saved == {"manifest_url": ""}

    def test_roundtrip(self):
        saved = {}
        with patch.object(uc, "update_config_section", side_effect=lambda s, v: saved.update(v)), \
             patch.object(uc, "get_config", return_value=None):
            uc.set_manifest_url("https://oss.example.com/aether/update-channel.json")
            assert saved["manifest_url"].startswith("https://")


# ==================== check_update ====================

class TestCheckUpdate:
    async def test_not_configured(self):
        with patch.object(uc, "get_manifest_url", return_value=""):
            result = await uc.check_update()
        assert result["status"] == "not_configured"

    async def test_up_to_date(self):
        def handler(request):
            return httpx.Response(200, json=_channel_payload(version="0.0.1"))
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"):
            result = await uc.check_update()
        assert result["status"] == "up_to_date"
        assert result["current"] == "1.0.0"

    async def test_available(self):
        def handler(request):
            return httpx.Response(200, json=_channel_payload(version="1.1.0"))
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"):
            result = await uc.check_update()
        assert result["status"] == "available"
        assert result["latest"]["version"] == "1.1.0"
        assert result["latest"]["size_bytes"] == 1024

    async def test_incompatible_when_min_compat_too_new(self):
        def handler(request):
            return httpx.Response(200, json=_channel_payload(version="2.0.0", min_compatible="1.5.0"))
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"):
            result = await uc.check_update()
        assert result["status"] == "incompatible"
        assert "1.5.0" in result["message"]

    async def test_fetch_failure_returns_error_state(self):
        def handler(request):
            return httpx.Response(500)
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"):
            result = await uc.check_update()
        assert result["status"] == "error"
        assert result["message"]


# ==================== 渠道清单解析 ====================

class TestChannelManifest:
    async def test_bad_json_rejected(self):
        def handler(request):
            return httpx.Response(200, content=b"not json{{{")
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/c.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)):
            with pytest.raises(AppException) as ei:
                await uc.fetch_channel_manifest()
        assert ei.value.code == "update_bad_manifest"

    async def test_missing_fields_rejected(self):
        def handler(request):
            return httpx.Response(200, json={"version": "1.1.0"})
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/c.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)):
            with pytest.raises(AppException):
                await uc.fetch_channel_manifest()

    async def test_oversize_rejected(self):
        def handler(request):
            return httpx.Response(200, content=b"x" * (uc.MANIFEST_MAX_BYTES + 1))
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/c.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)):
            with pytest.raises(AppException):
                await uc.fetch_channel_manifest()

    def test_resolve_pack_url(self):
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/aether/update-channel.json"):
            assert uc.resolve_pack_url({"pack": "aether-update-1.1.0.tar.gz"}) == \
                "https://x.io/aether/aether-update-1.1.0.tar.gz"
            assert uc.resolve_pack_url({"pack": "https://mirror.io/pack.tar.gz"}) == \
                "https://mirror.io/pack.tar.gz"


# ==================== download_and_apply ====================

class TestDownloadAndApply:
    async def test_downloads_verifies_and_applies(self):
        pack_bytes = b"fake-pack-content" * 100
        pack_sha = hashlib.sha256(pack_bytes).hexdigest()
        channel = _channel_payload(pack_sha256=pack_sha)

        def handler(request):
            if request.url.path.endswith("channel.json"):
                return httpx.Response(200, json=channel)
            return httpx.Response(200, content=pack_bytes)

        applied = {}

        async def fake_apply(path, operator):
            # download_and_apply 返回后会清理临时目录，内容必须在这里取
            applied["content"] = path.read_bytes()
            applied["operator"] = operator
            return {"from_version": "1.0.0", "to_version": "1.1.0", "restarting": True}

        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"), \
             patch.object(uc.upgrade, "apply_upgrade", fake_apply), \
             patch.object(uc, "audit") as mock_audit:
            result = await uc.download_and_apply("tester")

        assert result["to_version"] == "1.1.0"
        # 交给 apply_upgrade 的就是渠道下载的原始内容
        assert applied["content"] == pack_bytes
        assert applied["operator"] == "tester"
        mock_audit.record.assert_called_once()
        assert mock_audit.record.call_args[0][1] == "update_channel_apply"

    async def test_sha_mismatch_aborts(self):
        channel = _channel_payload(pack_sha256="0" * 64)

        def handler(request):
            if request.url.path.endswith("channel.json"):
                return httpx.Response(200, json=channel)
            return httpx.Response(200, content=b"corrupted-or-tampered")

        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"), \
             patch.object(uc.upgrade, "apply_upgrade", AsyncMock()) as mock_apply:
            with pytest.raises(AppException) as ei:
                await uc.download_and_apply("tester")
        assert ei.value.code == "update_sha_mismatch"
        mock_apply.assert_not_awaited()

    async def test_size_hint_over_limit_rejected(self):
        channel = _channel_payload(size_bytes=5 * 1024**3)
        def handler(request):
            return httpx.Response(200, json=channel)
        with patch.object(uc, "get_manifest_url", return_value="https://x.io/channel.json"), \
             patch.object(uc.httpx, "AsyncClient", _client_factory(handler)), \
             patch.object(uc, "get_version", return_value="1.0.0"):
            with pytest.raises(AppException) as ei:
                await uc.download_and_apply("tester")
        assert ei.value.code == "update_pack_too_large"
