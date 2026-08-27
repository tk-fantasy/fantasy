"""网络目标安全校验（供用户可控 URL/IP 的接口复用）。

背景：web_tools（LLM 可调用的 fetch_webpage）有完整的 SSRF 防护（禁内网/禁
rebinding），但产品自身的配置类接口（模型试连、RTSP 试连、HA 地址保存、
摄像头手动指 IP）合法目标本来就是内网——HA、ollama、IPC 全在局域网，不能
照搬"禁内网"策略，否则砍掉产品核心用法。

因此本模块只做对合法用法零影响的收敛：
- URL scheme 白名单：http/https（模型与 HA）、rtsp/rtsps/rtmp（摄像头）。
  阻断 file://、concat:、pipe:、gopher: 等 FFmpeg/httpx 能打开的任意协议
  （file:// 可读本地文件，是最实际的攻击面）。
- manual-ip 白名单：必须是 IPv4 且处于私网段/回环——摄像头发现功能语义上
  只该指向局域网设备，公网 IP 直接拒绝。
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

HTTP_SCHEMES = {"http", "https"}
STREAM_SCHEMES = {"rtsp", "rtsps", "rtmp", "http", "https"}


def url_scheme_error(url: str, allowed: set[str]) -> str | None:
    """校验 URL scheme。通过返回 None，不通过返回给用户的错误信息。"""
    try:
        scheme = (urlparse(url).scheme or "").lower()
    except ValueError:
        return "URL 格式无效"
    if scheme not in allowed:
        return f"只允许 {'/'.join(sorted(allowed))} 地址，收到: {scheme or '空'}"
    return None


def is_lan_ipv4(host: str) -> bool:
    """是否为 IPv4 且处于私网段/回环（摄像头发现语义：只指向局域网设备）。"""
    try:
        ip = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local
