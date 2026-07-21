"""配置项保存前预校验（probe）—— 4 个服务的「真连一次才允许落盘」逻辑。

每个 probe 接受**候选凭证作参数**（不读 config，避免「先写脏数据再回滚」的脏写
竞态），独立建临时连接，失败时按 reason 分类返回。复用 HA 那套模式：
    ProbeResult.ok=True       → 允许保存
    ProbeResult.ok=False      → 路由层拒绝，前端按 reason 展示差异化提示

reason 取值（前端按这个分支显示文案）：
    "unauthorized"  凭证无效/过期（URL/IP 可达，但鉴权失败）
    "unreachable"   地址不可达（DNS/连接/超时）
    "bad_format"    格式错误（schema 层已挡掉绝大多数，这里兜底）
    "busy"          资源被占（RTSP 单流被既有连接占用）
    "error"         其他（带原始异常文本）
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

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


# ============================ RTSP 摄像头 ============================

_RTSP_PROBE_TIMEOUT = 12.0


def _probe_rtsp_sync(url: str) -> ProbeResult:
    """同步拉一次 RTSP 流。路由层用 asyncio.to_thread 包。

    cv2/ffmpeg 的 C 层 warning 直接写 fd 2，绕过 Python 的 sys.stderr
    重定向，所以无法可靠地从 warning 文本里区分鉴权失败/不可达/单流占用。
    改用两步策略：
      1. 先裸 TCP 连 554 端口（或 URL 里的端口）—— 连不通 = unreachable
      2. 端口通但 cv2 打不开 = unauthorized 或 busy（detail 里说明）
    这样至少把「地址不通」和「凭证/资源问题」分开，前者修 URL，后者修凭证/停占流。
    """
    import socket
    from urllib.parse import urlparse

    import cv2

    # 解析 host/port 做端口探测
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 554
    if not host:
        return ProbeResult(ok=False, reason="bad_format", detail=f"URL 解析不到 host：{url}")

    # 第 1 步：TCP 端口可达性
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(4)
    try:
        sock.connect((host, port))
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return ProbeResult(
            ok=False, reason="unreachable",
            detail=f"摄像头 {host}:{port} 不可达：{e}",
        )
    finally:
        sock.close()

    # 第 2 步：端口通了，用 cv2 拉流验证凭证 + 流路
    # 跟 _open_network_stream 一致的低延迟参数 + TCP
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        "rtsp_transport;tcp"
        "|buffer_size;256k"
        "|max_delay;100000"
        "|fflags;nobuffer+discardcorrupt"
        "|flags;low_delay"
    )

    cap = None
    try:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            # 端口通但 cv2 打不开：凭证错、路径错、或单流被占
            return ProbeResult(
                ok=False, reason="unauthorized",
                detail=(
                    "摄像头端口可达但无法开流（常见原因：用户名/密码错、"
                    "RTSP 路径错、或单流已被其他客户端占用）"
                ),
            )
        # isOpened=True，再读 1 帧确认（有些摄像头 isOpened=True 但 read 失败）
        for _ in range(5):
            ok, frame = cap.read()
            if ok and frame is not None:
                h, w = frame.shape[:2]
                return ProbeResult(
                    ok=True,
                    detail=f"RTSP 连接成功，分辨率 {w}x{h}",
                    extra={"frame_size": [w, h]},
                )
            time.sleep(0.3)
        return ProbeResult(
            ok=False, reason="busy",
            detail="RTSP 流已打开但读不到帧（可能是单流被其他客户端占用）",
        )
    except Exception as e:
        return ProbeResult(ok=False, reason="error", detail=f"RTSP 探测异常：{e}")
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


async def probe_rtsp(url: str, username: str, password: str) -> ProbeResult:
    """验证 RTSP 凭证 + URL：用候选凭证拼完整 URL，cv2 开流读 1 帧。"""
    url = (url or "").strip()
    if not url:
        return ProbeResult(ok=False, reason="bad_format", detail="RTSP URL 不能为空")
    if not url.startswith("rtsp://"):
        return ProbeResult(ok=False, reason="bad_format", detail="URL 必须以 rtsp:// 开头")

    # 拼完整 URL（跟 _resolve_rtsp_url 一致的逻辑，但用候选凭证）
    full_url = url
    user = (username or "").strip()
    pwd = (password or "").strip()
    if user and pwd and "://" in url:
        scheme, rest = url.split("://", 1)
        # 去掉可能已存在的凭证
        if "@" in rest.split("/", 1)[0]:
            host_part = rest.split("/", 1)
            host_only = host_part[0].split("@", 1)[1]
            rest = host_only + ("/" + host_part[1] if len(host_part) > 1 else "")
        full_url = f"{scheme}://{user}:{pwd}@{rest}"

    # 同步 cv2 调用放线程池，避免阻塞事件循环
    return await asyncio.to_thread(_probe_rtsp_sync, full_url)


# ============================ PTZ 云台（ONVIF）============================

_PTZ_PROBE_TIMEOUT = 8.0


async def probe_ptz(ip: str, port: int, username: str, password: str) -> ProbeResult:
    """验证 ONVIF PTZ 凭证：构造临时 ONVIFCamera，update_xaddrs + GetProfiles。

    不复用 ptz_service 单例（避免污染运行中的连接状态）。
    """
    ip = (ip or "").strip()
    if not ip:
        return ProbeResult(ok=False, reason="bad_format", detail="PTZ IP 不能为空")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return ProbeResult(ok=False, reason="bad_format", detail="PTZ 端口必须是数字")

    try:
        import onvif
        from onvif import ONVIFCamera
    except ImportError:
        return ProbeResult(
            ok=False, reason="error",
            detail="ONVIF 库未安装，无法验证 PTZ 配置",
        )

    wsdl_dir = os.path.join(os.path.dirname(onvif.__file__), "wsdl")
    cam = None
    try:
        # 用 asyncio.waitaround 包超时（onvif-zeep-async 的 update_xaddrs 没有自带超时）
        async def _connect() -> ProbeResult:
            nonlocal cam
            cam = ONVIFCamera(ip, port, (username or "").strip(), (password or "").strip(), wsdl_dir=wsdl_dir)
            await cam.update_xaddrs()
            media = await cam.create_media_service()
            profiles = await media.GetProfiles()
            if not profiles:
                return ProbeResult(
                    ok=False, reason="error",
                    detail="摄像头可达但无媒体 profile（可能不支持 PTZ）",
                )
            return ProbeResult(
                ok=True,
                detail=f"PTZ 连接成功，profile token={profiles[0].token}",
                extra={"profile_count": len(profiles)},
            )

        return await asyncio.wait_for(_connect(), timeout=_PTZ_PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return ProbeResult(
            ok=False, reason="unreachable",
            detail=f"PTZ 连接超时（{_PTZ_PROBE_TIMEOUT}s），请检查 IP/端口是否可达",
        )
    except Exception as e:
        msg = str(e).lower()
        # onvif 鉴权失败通常表现为 zeep 抛 "not authorized" 或 Fault
        if "auth" in msg or "not authorized" in msg or "unauthorized" in msg or "401" in msg:
            return ProbeResult(
                ok=False, reason="unauthorized",
                detail=f"PTZ 凭证无效：{e}",
            )
        if "connection" in msg or "refused" in msg or "timeout" in msg or "resolve" in msg:
            return ProbeResult(
                ok=False, reason="unreachable",
                detail=f"PTZ 不可达：{e}",
            )
        return ProbeResult(ok=False, reason="error", detail=f"PTZ 探测失败：{e}")
    finally:
        # 临时连接不需要保持，清理掉
        cam = None


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
