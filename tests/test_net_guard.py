"""net_guard 与安全收敛项的回归测试。

覆盖：URL scheme 白名单（模型试连/HA 配置/RTSP 试连共用）、摄像头手动指 IP
的局域网校验、高级配置 GET 的 exa key 脱敏 + POST 留空保留语义。
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from app.core.net_guard import url_scheme_error, is_lan_ipv4, HTTP_SCHEMES, STREAM_SCHEMES


class TestUrlSchemeGuard:
    def test_http_https_allowed(self):
        assert url_scheme_error("http://192.168.1.5:11434/v1", HTTP_SCHEMES) is None
        assert url_scheme_error("https://api.example.com/v1", HTTP_SCHEMES) is None

    def test_file_scheme_blocked(self):
        # file:// 可被 FFmpeg 打开读本地文件，必须拦
        assert url_scheme_error("file:///etc/passwd", HTTP_SCHEMES) is not None
        assert url_scheme_error("file:///c/windows/win.ini", STREAM_SCHEMES) is not None

    def test_ffmpeg_arbitrary_protocols_blocked(self):
        for url in ("concat:///x", "pipe:0", "gopher://x", "ftp://x", "tcp://127.0.0.1:6379"):
            assert url_scheme_error(url, STREAM_SCHEMES) is not None, url

    def test_rtsp_allowed_for_stream(self):
        assert url_scheme_error("rtsp://192.168.4.32:554/stream2", STREAM_SCHEMES) is None
        assert url_scheme_error("rtsps://cam.example.com/stream", STREAM_SCHEMES) is None

    def test_scheme_case_insensitive(self):
        assert url_scheme_error("RTSP://192.168.1.1/stream", STREAM_SCHEMES) is None

    def test_empty_and_garbage(self):
        assert url_scheme_error("", HTTP_SCHEMES) is not None
        assert url_scheme_error("not a url", HTTP_SCHEMES) is not None


class TestLanIpv4:
    def test_private_and_loopback_allowed(self):
        for ip in ("192.168.1.10", "10.0.0.5", "172.17.0.3", "127.0.0.1"):
            assert is_lan_ipv4(ip) is True, ip

    def test_public_blocked(self):
        for ip in ("8.8.8.8", "1.1.1.1", "100.100.100.200"):
            assert is_lan_ipv4(ip) is False, ip

    def test_hostname_and_ipv6_blocked(self):
        for host in ("example.com", "", "fd00::1", "192.168.1.0/24", "300.1.1.1"):
            assert is_lan_ipv4(host) is False, host


class TestAdvancedConfigExaMasking:
    """GET 不回 exa key 明文；POST 留空保留旧值（防"打开面板→保存"清空 key）。"""

    @pytest.mark.asyncio
    async def test_get_masks_api_key(self):
        from app.routes import advanced_routes

        def fake_get(path, default=None):
            if path == "web_search.exa.api_key":
                return "sk-secret-value"
            if path == "vision":
                return {}
            return default

        with patch.object(advanced_routes, "get_config", side_effect=fake_get):
            result = await advanced_routes.get_advanced_config()

        exa = result.data["web_search"]["exa"]
        assert exa["api_key"] == ""
        assert exa["has_exa_key"] is True
        assert "sk-secret-value" not in str(result.data)

    @pytest.mark.asyncio
    async def test_post_blank_key_preserves_saved_key(self):
        from app.routes import advanced_routes
        from app.schema.api_schemas import AdvancedConfigRequest, WebSearchConfig

        captured = {}

        def fake_update(section, data):
            captured[section] = data

        def fake_get(path, default=None):
            if path == "web_search.exa.api_key":
                return "sk-old-key"
            return default

        req = AdvancedConfigRequest(
            web_search=WebSearchConfig(exa={"api_key": ""}),
        )
        with patch.object(advanced_routes, "get_config", side_effect=fake_get), \
             patch.object(advanced_routes, "update_config_section", side_effect=fake_update):
            result = await advanced_routes.set_advanced_config(req)

        assert result.data["saved"] is True
        assert captured["web_search"]["exa"]["api_key"] == "sk-old-key"

    @pytest.mark.asyncio
    async def test_post_new_key_probed_and_saved(self):
        from app.routes import advanced_routes
        from app.schema.api_schemas import AdvancedConfigRequest, WebSearchConfig

        captured = {}

        class _ProbeResult:
            ok = True
            reason = ""
            detail = ""
            def to_dict(self):
                return {}

        with patch.object(advanced_routes, "probe_exa", return_value=_ProbeResult()), \
             patch.object(advanced_routes, "update_config_section",
                          side_effect=lambda s, d: captured.update({s: d})):
            req = AdvancedConfigRequest(web_search=WebSearchConfig(exa={"api_key": "sk-new-key-0123456789abcdef"}))
            result = await advanced_routes.set_advanced_config(req)

        assert result.data["saved"] is True
        assert captured["web_search"]["exa"]["api_key"] == "sk-new-key-0123456789abcdef"
