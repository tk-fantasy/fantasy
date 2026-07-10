"""Tests for web tools SSRF protection and fetching."""
from __future__ import annotations

import ipaddress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.web_tools import (
    _is_ip_blocked,
    _is_blocked_host,
    _validate_url,
    _extract_text,
    _extract_markdown,
    _convert,
    fetch_webpage_handler,
    http_request_handler,
)


class TestIsIpBlocked:
    def test_private_ip_blocked(self):
        assert _is_ip_blocked(ipaddress.ip_address("192.168.1.1")) is True
        assert _is_ip_blocked(ipaddress.ip_address("10.0.0.1")) is True
        assert _is_ip_blocked(ipaddress.ip_address("172.16.0.1")) is True

    def test_loopback_blocked(self):
        assert _is_ip_blocked(ipaddress.ip_address("127.0.0.1")) is True
        assert _is_ip_blocked(ipaddress.ip_address("::1")) is True

    def test_link_local_blocked(self):
        assert _is_ip_blocked(ipaddress.ip_address("169.254.1.1")) is True

    def test_public_ip_allowed(self):
        assert _is_ip_blocked(ipaddress.ip_address("8.8.8.8")) is False
        assert _is_ip_blocked(ipaddress.ip_address("1.1.1.1")) is False
        assert _is_ip_blocked(ipaddress.ip_address("93.184.216.34")) is False


class TestValidateUrl:
    def test_valid_http_url(self):
        with patch("app.mcp.web_tools._is_blocked_host", return_value=False):
            result = _validate_url("http://example.com/path")
            assert result is None

    def test_valid_https_url(self):
        with patch("app.mcp.web_tools._is_blocked_host", return_value=False):
            result = _validate_url("https://example.com/path")
            assert result is None

    def test_invalid_scheme(self):
        result = _validate_url("ftp://example.com/file")
        assert result is not None
        assert "只允许 http/https" in result

    def test_missing_hostname(self):
        result = _validate_url("http:///path")
        assert result is not None
        assert "缺少主机名" in result

    def test_blocked_host(self):
        with patch("app.mcp.web_tools._is_blocked_host", return_value=True):
            result = _validate_url("http://internal.server/path")
            assert result is not None
            assert "安全" in result


class TestExtractText:
    def test_removes_script_tags(self):
        html = "<html><body><script>alert('xss')</script><p>Content</p></body></html>"
        text = _extract_text(html)
        assert "alert" not in text
        assert "Content" in text

    def test_removes_style_tags(self):
        html = "<html><head><style>.red{color:red}</style></head><body>Text</body></html>"
        text = _extract_text(html)
        assert ".red" not in text
        assert "Text" in text

    def test_removes_html_tags(self):
        html = "<p>Hello <strong>World</strong></p>"
        text = _extract_text(html)
        assert "Hello" in text
        assert "World" in text
        assert "<" not in text

    def test_replaces_entities(self):
        html = "<p>A &amp; B &lt; C &gt; D &quot;E&quot; &#39;F&#39;</p>"
        text = _extract_text(html)
        assert "&" in text
        assert "<" in text
        assert ">" in text
        assert '"' in text
        assert "'" in text

    def test_collapses_whitespace(self):
        html = "<p>A\n\n\n\nB</p>"
        text = _extract_text(html)
        assert "\n\n\n" not in text


class TestExtractMarkdown:
    """markdown 输出保留结构。对标 opencode convertHTMLToMarkdown。"""

    def test_preserves_headings(self):
        html = "<html><body><h1>Title</h1><p>Body</p></body></html>"
        md = _extract_markdown(html)
        assert "# Title" in md
        assert "Body" in md

    def test_preserves_lists(self):
        html = "<ul><li>apple</li><li>banana</li></ul>"
        md = _extract_markdown(html)
        assert "apple" in md
        assert "banana" in md
        # markdown 列表项标记
        assert "*" in md or "-" in md

    def test_preserves_emphasis(self):
        html = "<p>Hello <strong>World</strong></p>"
        md = _extract_markdown(html)
        assert "**World**" in md

    def test_drops_script_style(self):
        html = "<html><body><script>alert(1)</script><style>x{}</style><p>ok</p></body></html>"
        md = _extract_markdown(html)
        assert "alert" not in md
        assert "{}" not in md
        assert "ok" in md


class TestConvert:
    """_convert 按 content_type + format 路由。对标 opencode convert()。"""

    def test_html_to_markdown(self):
        html = "<h1>T</h1>"
        assert "# T" in _convert(html, "text/html", "markdown")

    def test_html_to_text(self):
        html = "<h1>T</h1>"
        assert "T" in _convert(html, "text/html", "text")
        assert "#" not in _convert(html, "text/html", "text")

    def test_non_html_passthrough(self):
        # 非 HTML 原样返回,不解析
        assert _convert("plain text", "text/plain", "markdown") == "plain text"


class TestFetchWebpageHandler:
    @pytest.mark.asyncio
    async def test_missing_url(self):
        result = await fetch_webpage_handler({}, None)
        assert "error" in result
        assert "url" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_adds_https_scheme(self):
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b"<html><title>Test</title><body>Content</body></html>"
                mock_response.encoding = "utf-8"
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.get = AsyncMock(return_value=mock_response)

                result = await fetch_webpage_handler({"url": "example.com"}, None)
                assert "error" not in result or result.get("status_code") == 200

    @pytest.mark.asyncio
    async def test_ssrf_blocked(self):
        with patch("app.mcp.web_tools._validate_url", return_value="出于安全考虑"):
            result = await fetch_webpage_handler({"url": "http://internal.server"}, None)
            assert "error" in result
            assert "安全" in result["error"]

    @pytest.mark.asyncio
    async def test_truncates_long_content(self):
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                long_content = "x" * 10000
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = f"<html><body>{long_content}</body></html>".encode()
                mock_response.encoding = "utf-8"
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.get = AsyncMock(return_value=mock_response)

                result = await fetch_webpage_handler({"url": "http://example.com", "max_chars": 100}, None)
                assert len(result.get("text", "")) <= 100
                assert result.get("truncated") is True


class TestFetchWebpageUserAgent:
    @pytest.mark.asyncio
    async def test_uses_browser_ua_by_default(self):
        """默认用真实浏览器 UA,降低反爬识别。"""
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b"<html><body>ok</body></html>"
                mock_response.encoding = "utf-8"
                mock_response.headers = {}
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.get = AsyncMock(return_value=mock_response)

                await fetch_webpage_handler({"url": "http://example.com"}, None)
                sent_headers = mock_client.return_value.get.call_args.kwargs["headers"]
                assert sent_headers["User-Agent"].startswith("Mozilla/5.0")

    @pytest.mark.asyncio
    async def test_cloudflare_challenge_triggers_ua_fallback(self):
        """遇 Cloudflare 挑战页(403 + cf-mitigated),回退到 bot UA 重试一次。"""
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                challenge = MagicMock()
                challenge.status_code = 403
                challenge.headers = {"cf-mitigated": "challenge"}

                ok = MagicMock()
                ok.status_code = 200
                ok.content = b"<html><body>content</body></html>"
                ok.encoding = "utf-8"
                ok.headers = {}
                ok.raise_for_status = MagicMock()

                mock_client.return_value.get = AsyncMock(side_effect=[challenge, ok])

                result = await fetch_webpage_handler({"url": "http://example.com"}, None)
                assert mock_client.return_value.get.call_count == 2
                # 第二次请求用的应是 bot UA
                second_headers = mock_client.return_value.get.call_args_list[1].kwargs["headers"]
                assert second_headers["User-Agent"] == "aether/1.0 (+local tool)"
                assert "content" in result["text"]


class TestFetchWebpageFormat:
    """format 参数 + MIME 过滤。对标 opencode webfetch。"""

    @pytest.mark.asyncio
    async def test_default_format_is_markdown(self):
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b"<html><body><h1>Title</h1><p>ok</p></body></html>"
                mock_response.encoding = "utf-8"
                mock_response.headers = {"Content-Type": "text/html"}
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.get = AsyncMock(return_value=mock_response)

                result = await fetch_webpage_handler({"url": "http://example.com"}, None)
                assert result["format"] == "markdown"
                assert "# Title" in result["text"]

    @pytest.mark.asyncio
    async def test_explicit_text_format(self):
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b"<html><body><h1>Title</h1></body></html>"
                mock_response.encoding = "utf-8"
                mock_response.headers = {"Content-Type": "text/html"}
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.get = AsyncMock(return_value=mock_response)

                result = await fetch_webpage_handler({"url": "http://example.com", "format": "text"}, None)
                assert result["format"] == "text"
                assert "Title" in result["text"]
                assert "#" not in result["text"]

    @pytest.mark.asyncio
    async def test_invalid_format_rejected(self):
        result = await fetch_webpage_handler({"url": "http://example.com", "format": "xml"}, None)
        assert "error" in result
        assert "format" in result["error"]

    @pytest.mark.asyncio
    async def test_non_text_mime_rejected(self):
        """图片等二进制 MIME 应拒收。对标 opencode isTextualMime。"""
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b"\x89PNG\r\n\x1a\n"
                mock_response.encoding = "utf-8"
                mock_response.headers = {"Content-Type": "image/png"}
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.get = AsyncMock(return_value=mock_response)

                result = await fetch_webpage_handler({"url": "http://example.com/img.png"}, None)
                assert "error" in result
                assert "image/png" in result["error"]


class TestHttpRequestHandler:
    @pytest.mark.asyncio
    async def test_missing_url(self):
        result = await http_request_handler({}, None)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_invalid_method(self):
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            result = await http_request_handler({"url": "http://example.com", "method": "INVALID"}, None)
            assert "error" in result
            assert "不支持" in result["error"]

    @pytest.mark.asyncio
    async def test_ssrf_blocked(self):
        with patch("app.mcp.web_tools._validate_url", return_value="出于安全考虑"):
            result = await http_request_handler({"url": "http://internal.server"}, None)
            assert "error" in result

    @pytest.mark.asyncio
    async def test_default_method_is_get(self):
        with patch("app.mcp.web_tools._validate_url", return_value=None):
            with patch("app.mcp.web_tools._get_http_client") as mock_client:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b'{"key": "value"}'
                mock_response.encoding = "utf-8"
                mock_response.headers = {"Content-Type": "application/json"}
                mock_response.raise_for_status = MagicMock()
                mock_client.return_value.request = AsyncMock(return_value=mock_response)

                result = await http_request_handler({"url": "http://api.example.com"}, None)
                call_args = mock_client.return_value.request.call_args
                assert call_args[0][0] == "GET"
