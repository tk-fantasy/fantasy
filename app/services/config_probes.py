"""配置项保存前预校验（probe）——「真连一次才允许落盘」逻辑。

每个 probe 接受**候选凭证作参数**（不读 config，避免「先写脏数据再回滚」的脏写
竞态），独立建临时连接，失败时按 reason 分类返回。复用 HA 那套模式：
    ProbeResult.ok=True       → 允许保存
    ProbeResult.ok=False      → 路由层拒绝，前端按 reason 展示差异化提示

reason 取值（前端按这个分支显示文案）：
    "unauthorized"  凭证无效/过期（URL 可达，但鉴权失败）
    "unreachable"   地址不可达（DNS/连接/超时）
    "bad_format"    格式错误（schema 层已挡掉绝大多数，这里兜底）
    "error"         其他（带原始异常文本）
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    """probe 统一返回。ok=False 时 reason/detail 必填。"""
    ok: bool
    reason: str = ""        # "unauthorized"|"unreachable"|"bad_format"|"busy"|"error"
    detail: str = ""        # 中文可读详情
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"ok": self.ok}
        if self.reason:
            d["reason"] = self.reason
        if self.detail:
            d["detail"] = self.detail
        if self.extra:
            d.update(self.extra)
        return d


# 通用：把 httpx 异常分类成 unreachable / unauthorized / error
def _classify_httpx_error(e: Exception) -> tuple[str, str]:
    """返回 (reason, detail)。供 Exa / 天气 / PTZ 复用。"""
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code in (401, 403):
            return ("unauthorized", f"凭证无效或已过期（HTTP {e.response.status_code}）")
        return ("error", f"服务返回 HTTP {e.response.status_code}")
    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, httpx.UnsupportedProtocol)):
        return ("unreachable", f"地址不可达：{e}")
    return ("error", str(e))


# ============================ Exa 网页搜索 ============================

_EXA_URL = "https://mcp.exa.ai/mcp"
_EXA_PROBE_TIMEOUT = 8.0


async def probe_exa(api_key: str) -> ProbeResult:
    """验证 Exa API key：用候选 key 发一个最小 web_search_exa 查询。

    Exa MCP 匿名也能用（有配额限制），所以 key 错了不一定返回 401，而是
    后续搜索被拒。这里用一个最小查询检查「key 被服务接受」—— 错 key 通常
    返回 4xx 或 result.error。
    """
    api_key = (api_key or "").strip()
    if not api_key:
        # 留空 = 匿名，不验证（跟现有逻辑一致）
        return ProbeResult(ok=True, extra={"anonymous": True})

    from urllib.parse import urlparse, urlencode, parse_qsl
    parsed = urlparse(_EXA_URL)
    params = dict(parse_qsl(parsed.query))
    params["exaApiKey"] = api_key
    url = parsed._replace(query=urlencode(params)).geturl()

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {"query": "test", "type": "auto", "numResults": 1},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_EXA_PROBE_TIMEOUT) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
            # 401/403 明确是 key 问题；4xx 其他也按 key 问题处理（Exa 对错 key 返回 403）
            if resp.status_code in (401, 403):
                return ProbeResult(
                    ok=False,
                    reason="unauthorized",
                    detail=f"Exa API key 无效或被拒（HTTP {resp.status_code}）",
                )
            if resp.status_code >= 400:
                return ProbeResult(
                    ok=False,
                    reason="error",
                    detail=f"Exa 返回 HTTP {resp.status_code}：{resp.text[:200]}",
                )
            # 2xx 但响应体里可能有错误（Exa 对错 key 也返回 200，错误藏在 body 里）。
            # Exa MCP 返回 SSE 格式：每行 "data: {json}"，json 里 result.isError=true
            # 或 result.content[].text 含 "error (401): Invalid API key"。
            body = resp.text
            # 先按 SSE 解析，回退到整块 JSON
            json_strs = []
            for line in body.splitlines():
                if line.startswith("data: "):
                    json_strs.append(line[6:].strip())
                elif line.strip().startswith("{"):
                    json_strs.append(line.strip())
            if not json_strs and body.strip().startswith("{"):
                json_strs.append(body.strip())

            for js in json_strs:
                try:
                    obj = json.loads(js)
                except json.JSONDecodeError:
                    continue
                # JSON-RPC error 字段
                if obj.get("error"):
                    err_msg = obj["error"].get("message", str(obj["error"]))
                    return ProbeResult(
                        ok=False,
                        reason="unauthorized",
                        detail=f"Exa 拒绝 key：{err_msg}",
                    )
                result = obj.get("result") or {}
                # isError 标志
                if result.get("isError"):
                    # 从 content[].text 提取错误文本
                    content = result.get("content") or []
                    err_text = ""
                    for item in content:
                        if isinstance(item, dict) and item.get("text"):
                            err_text = item["text"]
                            break
                    # "Invalid API key" / "401" 明确是 key 问题
                    if "401" in err_text or "invalid api key" in err_text.lower() or "api key" in err_text.lower():
                        return ProbeResult(
                            ok=False,
                            reason="unauthorized",
                            detail=f"Exa API key 无效：{err_text[:120]}",
                        )
                    return ProbeResult(
                        ok=False,
                        reason="error",
                        detail=f"Exa 返回错误：{err_text[:120]}",
                    )
            return ProbeResult(ok=True, detail="Exa key 验证通过")
    except Exception as e:
        reason, detail = _classify_httpx_error(e)
        logger.warning("Exa probe failed: %s (%s)", reason, detail)
        return ProbeResult(ok=False, reason=reason, detail=detail)


# ============================ 天气（和风）============================

_WEATHER_PROBE_TIMEOUT = 8.0


def _build_qweather_jwt(host: str, kid: str, sub: str, private_key_b64: str) -> str:
    """用候选凭证生成 JWT（复用 weather_service._generate_jwt 的逻辑但参数化）。"""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    if not private_key_b64:
        raise ValueError("private_key 未配置")
    if not kid:
        raise ValueError("kid 未配置")
    if not sub:
        raise ValueError("sub 未配置")

    if not private_key_b64.startswith("-----"):
        key_lines = [private_key_b64[i:i+64] for i in range(0, len(private_key_b64), 64)]
        pem = (
            b"-----BEGIN PRIVATE KEY-----\n"
            + "\n".join(key_lines).encode()
            + b"\n-----END PRIVATE KEY-----"
        )
    else:
        pem = private_key_b64.encode()

    private_key = load_pem_private_key(pem, password=None)

    def b64u(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    header = b64u(json.dumps({"alg": "EdDSA", "kid": kid}, separators=(",", ":")).encode())
    now = int(time.time())
    payload = b64u(json.dumps({
        "sub": sub,
        "iat": now - 30,
        "exp": now + 900,
    }, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = b64u(private_key.sign(signing_input))
    return f"{header}.{payload}.{signature}"


async def probe_weather(host: str, kid: str, sub: str, private_key: str) -> ProbeResult:
    """验证和风天气凭证：用候选凭证生成 JWT，GET /geo/v2/city/lookup?location=auto。"""
    host = (host or "").strip()
    if not host:
        return ProbeResult(ok=False, reason="bad_format", detail="天气 host 不能为空")

    # 先验证 JWT 能否生成（private_key 格式错的会在这步抛）
    try:
        token = _build_qweather_jwt(host, kid, sub, private_key)
    except ValueError as e:
        return ProbeResult(ok=False, reason="bad_format", detail=f"凭证格式错误：{e}")
    except Exception as e:
        return ProbeResult(ok=False, reason="bad_format", detail=f"private_key 无效：{e}")

    url = f"https://{host}/geo/v2/city/lookup"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=_WEATHER_PROBE_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params={"location": "auto"})
            if resp.status_code in (401, 403):
                return ProbeResult(
                    ok=False, reason="unauthorized",
                    detail=f"和风凭证无效（HTTP {resp.status_code}），请检查 kid/sub/private_key",
                )
            if resp.status_code >= 400:
                return ProbeResult(
                    ok=False, reason="error",
                    detail=f"和风返回 HTTP {resp.status_code}：{resp.text[:200]}",
                )
            return ProbeResult(ok=True, detail="和风天气凭证验证通过")
    except Exception as e:
        reason, detail = _classify_httpx_error(e)
        logger.warning("Weather probe failed: %s (%s)", reason, detail)
        return ProbeResult(ok=False, reason=reason, detail=detail)
