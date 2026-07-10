"""网页搜索工具。

调用 Exa MCP 端点 (https://mcp.exa.ai/mcp) 搜索网页，返回格式化好的上下文文本。
匿名可用；配置 api_key 可提配额。反爬由 Exa 后端处理。
协议参考 opencode 的实现 (packages/core/src/tool/websearch.ts)。
"""
from __future__ import annotations

import json
import logging

import httpx

from ..core.config import get_config
from ..clients.llm_base_client import _get_shared_client

logger = logging.getLogger(__name__)

_MAX_RESULTS = 5

# Exa MCP 端点 (匿名可用；带 api_key 时拼到 query string 提配额)
_EXA_URL = "https://mcp.exa.ai/mcp"
_EXA_TIMEOUT = 25  # opencode 用 25s


async def web_search_handler(parameters: dict, session) -> dict:
    """搜索网页。参数 query(必填)、max_results(可选,默认5)。

    通过 Exa MCP 的 web_search_exa 工具搜索，返回其格式化好的上下文文本。
    """
    query = str(parameters.get("query", "")).strip()
    if not query:
        return {"error": "缺少 query 参数"}

    max_results = int(parameters.get("max_results", _MAX_RESULTS) or _MAX_RESULTS)
    max_results = max(1, min(max_results, 10))

    return await _search_exa(query, max_results)


def _exa_url(api_key: str | None) -> str:
    """无 key 时匿名调用；有 key 时拼到 URL 提配额。与 opencode 一致。"""
    if not api_key:
        return _EXA_URL
    from urllib.parse import urlparse, urlencode, parse_qsl
    parsed = urlparse(_EXA_URL)
    params = dict(parse_qsl(parsed.query))
    params["exaApiKey"] = api_key
    return parsed._replace(query=urlencode(params)).geturl()


def _parse_mcp_text(body: str) -> str | None:
    """从 MCP 响应里抽取 content[].text。

    支持两种形态:整块 JSON,或 SSE 的多行 'data: <json>'。
    """
    def _extract(payload: str) -> str | None:
        payload = payload.strip()
        if not payload.startswith("{"):
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        content = (obj.get("result") or {}).get("content") or []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return item["text"]
        return None

    direct = _extract(body)
    if direct:
        return direct
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        text = _extract(line[6:])
        if text:
            return text
    return None


async def _search_exa(query: str, max_results: int) -> dict:
    """调用 Exa MCP 的 web_search_exa 工具,返回其格式化好的上下文文本。"""
    api_key = get_config("web_search.exa.api_key", "") or None
    url = _exa_url(api_key)
    # MCP JSON-RPC 请求体,与 opencode 的 McpRequest 结构一致
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {
                "query": query,
                "type": "auto",
                "numResults": max_results,
                "livecrawl": "fallback",
            },
        },
    }
    client = _get_shared_client()
    try:
        resp = await client.post(
            url,
            json=payload,
            headers={"Accept": "application/json, text/event-stream"},
            timeout=_EXA_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Exa 搜索失败: %s", exc)
        return {"error": f"Exa 搜索失败: {exc}", "query": query}

    text = _parse_mcp_text(resp.text)
    if not text:
        return {
            "query": query,
            "results": [],
            "message": "Exa 未返回结果",
        }
    return {"query": query, "text": text}
