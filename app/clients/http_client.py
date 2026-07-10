"""HTTP 客户端工厂 — 统一 trust_env=False，绕过系统代理。

所有 httpx 客户端创建都经过此模块，确保：
1. 不读取系统代理环境变量（HTTP_PROXY / HTTPS_PROXY）
2. 超时、重定向等默认值统一
3. 代理策略只在此处修改一处
"""
from __future__ import annotations

import httpx


def new_client(
    timeout: float = 10.0,
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    **kwargs,
) -> httpx.AsyncClient:
    """创建异步 httpx 客户端（绕过系统代理）。"""
    return httpx.AsyncClient(
        timeout=timeout,
        base_url=base_url,
        headers=headers or {},
        follow_redirects=follow_redirects,
        trust_env=False,
        **kwargs,
    )


def new_sync_client(
    timeout: float = 20.0,
    **kwargs,
) -> httpx.Client:
    """创建同步 httpx 客户端（绕过系统代理）。"""
    return httpx.Client(timeout=timeout, trust_env=False, **kwargs)
