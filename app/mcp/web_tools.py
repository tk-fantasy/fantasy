"""通用工具:网页爬取、HTTP 请求。

使用 httpx.AsyncClient 实现异步 HTTP，
通过自定义 transport 在连接前校验 DNS 解析结果，防止 SSRF 和 DNS rebinding 攻击。
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from urllib.parse import urlparse

import httpx

# 抓取/请求的安全与体积限制
_MAX_BYTES = 5_000_000         # 单次最多读取 5MB 正文(对标 opencode webfetch)
_TIMEOUT = 30                  # 秒(对标 opencode 默认 30s)
# 真实浏览器 UA,降低被反爬一眼识破的概率(对标 opencode webfetch)
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
# bot UA 回退用(opencode 在遭遇 Cloudflare 挑战时回退到自己的 UA)
_USER_AGENT = "aether/1.0 (+local tool)"
_ALLOWED_SCHEMES = {"http", "https"}


def _is_cloudflare_challenge(resp: httpx.Response) -> bool:
    """Cloudflare 挑战页:403 + cf-mitigated: challenge 头。对标 opencode。"""
    if resp.status_code != 403:
        return False
    return resp.headers.get("cf-mitigated", "").lower() == "challenge"


# ----------------------------------------------------------- SSRF 防护

def _is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """检查 IP 是否为内网/本机地址。"""
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _is_blocked_host(hostname: str) -> bool:
    """拦截指向内网/本机的地址,避免被诱导请求内部服务。"""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False  # 解析不了交给 httpx 自己报错
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_ip_blocked(ip):
            return True
    return False


class _SafeTransport(httpx.AsyncHTTPTransport):
    """自定义 transport，在连接前校验 DNS 解析结果，防止 DNS rebinding。"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # 在连接前校验目标 IP
        host = request.url.host
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # 解析失败交给 httpx 处理
            return await super().handle_async_request(request)

        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if _is_ip_blocked(ip):
                raise httpx.ConnectError(
                    f"出于安全考虑,禁止访问内网/本机地址: {host} -> {ip}"
                )
        return await super().handle_async_request(request)


def _validate_url(url: str) -> str | None:
    """通过返回 None,不通过返回错误信息。"""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return f"只允许 http/https,收到: {parsed.scheme or '空'}"
    if not parsed.hostname:
        return "URL 缺少主机名"
    if _is_blocked_host(parsed.hostname):
        return "出于安全考虑,禁止访问内网/本机地址"
    return None


def _make_redirect_hook() -> list:
    """创建 httpx event hooks，在每个重定向跳转前校验目标地址。

    httpx 的 response hook 签名是 (response)，不传 request。
    """
    async def _check_redirect(response):
        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if location:
                # 处理相对路径重定向
                if location.startswith("/"):
                    return  # 同域重定向，已在初始校验中覆盖
                err = _validate_url(location)
                if err:
                    raise httpx.RequestError(f"重定向目标被拦截: {err}")

    return [_check_redirect]


# 复用 httpx 客户端，避免每次请求重建连接池
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            transport=_SafeTransport(),
            event_hooks={"response": _make_redirect_hook()},
        )
    return _http_client


async def close_http_client() -> None:
    """关闭共享的 httpx 客户端，释放连接池资源。"""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# --------------------------------------------------------------- 网页爬取

from markdownify import markdownify
from html.parser import HTMLParser

# 纯文本模式下要跳过的标签(对标 opencode extractTextFromHTML 的 skip 列表)
_SKIP_TAGS = {"script", "style", "noscript", "iframe", "object", "embed"}


class _TextExtractor(HTMLParser):
    """从 HTML 提取纯文本:跳过 script/style 等,只收文本节点。

    对标 opencode 的 extractTextFromHTML(htmlparser2 实现)。
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._skip_depth > 0 or tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def _extract_text(html: str) -> str:
    """从 HTML 提取纯文本:去 script/style/标签,不保留任何 markdown 语法。对标 opencode extractTextFromHTML。"""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    # 压缩连续空行(避免大段空白),对标 opencode .trim()
    lines = [line.rstrip() for line in parser.get_text().splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _extract_markdown(html: str) -> str:
    """把 HTML 转成 markdown,保留标题/列表/链接/强调结构。对标 opencode convertHTMLToMarkdown。"""
    # heading_style=ATX 输出 "# 标题"（默认 SETEXT 是 "标题\n==="，可读性差）
    # markdownify 默认不折行;strip=['img'] 对标原 html2text 的 ignore_images
    return markdownify(html, strip=['img'], heading_style="ATX").strip()


def _convert(html: str, content_type: str, fmt: str) -> str:
    """按目标格式转换内容。非 HTML 原样返回。对标 opencode convert()。"""
    if "text/html" not in content_type:
        return html
    if fmt == "markdown":
        return _extract_markdown(html)
    if fmt == "text":
        return _extract_text(html)
    return html


def _mime_from(content_type: str) -> str:
    """取 MIME 主类型。对标 opencode mimeFrom()。"""
    return content_type.split(";", 1)[0].strip().lower()


def _is_textual_mime(mime: str) -> bool:
    """是否为可处理的文本类 MIME。对标 opencode isTextualMime()。"""
    return (
        not mime
        or mime.startswith("text/")
        or mime == "application/json"
        or mime.endswith("+json")
        or mime == "application/xml"
        or mime.endswith("+xml")
        or mime in ("application/javascript", "application/x-javascript")
    )


async def _fetch(url: str, user_agent: str) -> httpx.Response:
    """单次 GET,带指定 UA。对标 opencode webfetch 的 request()。"""
    client = _get_http_client()
    return await client.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


async def fetch_webpage_handler(parameters: dict, session) -> dict:
    """抓取网页并返回正文。参数 url(必填)、max_chars(可选,默认 4000)、format(可选,text/markdown,默认 markdown)。

    默认用真实浏览器 UA;遭遇 Cloudflare 挑战页时回退到 bot UA 重试(对标 opencode)。
    HTML 按 format 转成 markdown 或纯文本;非文本 MIME 拒收(对标 opencode)。
    """
    url = str(parameters.get("url", "")).strip()
    if not url:
        return {"error": "缺少 url 参数"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    err = _validate_url(url)
    if err:
        return {"error": err}
    max_chars = int(parameters.get("max_chars", 4000) or 4000)
    fmt = str(parameters.get("format", "markdown")).lower()
    if fmt not in ("text", "markdown"):
        return {"error": f"不支持的 format: {fmt}(可选 text/markdown)"}
    try:
        resp = await _fetch(url, _BROWSER_UA)
        # Cloudflare 挑战页:换 bot UA 回退重试一次(opencode 同款逻辑)
        if _is_cloudflare_challenge(resp):
            resp = await _fetch(url, _USER_AGENT)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        mime = _mime_from(content_type)
        if not _is_textual_mime(mime):
            return {"error": f"不支持的响应类型: {mime or '未知'}", "url": url}
        raw = resp.content[:_MAX_BYTES]
        encoding = resp.encoding or "utf-8"
        html = raw.decode(encoding, errors="replace")
        status_code = resp.status_code
    except httpx.HTTPError as exc:
        return {"error": f"抓取失败: {exc}", "url": url}

    title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else ""
    text = _convert(html, content_type, fmt)
    truncated = len(text) > max_chars
    return {
        "url": url,
        "status_code": status_code,
        "title": title,
        "format": fmt,
        "text": text[:max_chars],
        "truncated": truncated,
        "length": len(text),
    }


# --------------------------------------------------------------- HTTP 请求

async def http_request_handler(parameters: dict, session) -> dict:
    """通用 HTTP 请求,适合调公开 API。

    参数:url(必填)、method(GET/POST/PUT/DELETE,默认 GET)、
    headers(可选 dict)、params(可选 dict,query)、json_body(可选,POST 的 JSON 体)。
    """
    url = str(parameters.get("url", "")).strip()
    if not url:
        return {"error": "缺少 url 参数"}
    err = _validate_url(url)
    if err:
        return {"error": err}
    method = str(parameters.get("method", "GET")).upper()
    if method not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
        return {"error": f"不支持的方法: {method}"}
    headers = parameters.get("headers") or {}
    headers.setdefault("User-Agent", _USER_AGENT)
    try:
        client = _get_http_client()
        resp = await client.request(
            method,
            url,
            headers=headers,
            params=parameters.get("params") or None,
            json=parameters.get("json_body") or None,
        )
        body_bytes = resp.content[:_MAX_BYTES]
        text = body_bytes.decode(resp.encoding or "utf-8", errors="replace")
        status_code = resp.status_code
        content_type = resp.headers.get("Content-Type", "")
    except httpx.HTTPError as exc:
        return {"error": f"请求失败: {exc}", "url": url}

    result: dict = {
        "url": url,
        "method": method,
        "status_code": status_code,
        "content_type": content_type,
    }
    # JSON 响应尝试解析,否则返回截断文本
    if "application/json" in result["content_type"].lower():
        try:
            result["json"] = json.loads(text)
        except json.JSONDecodeError:
            result["text"] = text[:4000]
    else:
        result["text"] = text[:4000]
    return result
