"""Home Assistant REST API 客户端。

支持两种认证方式:
1. Long-Lived Access Token (推荐)
2. trusted_networks (本地免认证)

API 文档: https://developers.home-assistant.io/docs/api/rest/
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..core.config import get_config
from .http_client import new_client

logger = logging.getLogger(__name__)

# HA 默认配置
DEFAULT_HA_URL = "http://localhost:8123"
DEFAULT_HA_TOKEN = ""


class HomeAssistantClient:
    """Home Assistant REST API 客户端。"""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self._base_url = (base_url or get_config("ha.url") or DEFAULT_HA_URL).rstrip("/")
        self._token = token or get_config("ha.token") or DEFAULT_HA_TOKEN
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None or self._client.is_closed:
                headers = {
                    "Content-Type": "application/json",
                }
                if self._token:
                    headers["Authorization"] = f"Bearer {self._token}"
                self._client = new_client(
                    timeout=10.0,
                    base_url=self._base_url,
                    headers=headers,
                )
            return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ============ 状态查询 ============

    async def get_states(self) -> list[dict[str, Any]]:
        """获取所有实体状态。"""
        client = await self._get_client()
        response = await client.get("/api/states")
        response.raise_for_status()
        return response.json()

    # ============ 服务调用 ============

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调用 HA 服务。

        Args:
            domain: 服务域 (light, climate, cover, etc.)
            service: 服务名 (turn_on, turn_off, set_temperature, etc.)
            entity_id: 目标实体 ID (可选)
            data: 额外服务数据 (可选)
        """
        client = await self._get_client()
        payload: dict[str, Any] = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if data:
            payload.update(data)

        response = await client.post(f"/api/services/{domain}/{service}", json=payload)
        response.raise_for_status()
        return response.json()

    # ============ 服务发现 ============

    async def get_services(self) -> list[dict[str, Any]]:
        """获取 HA 所有可用服务定义（含各服务接受的 fields）。

        API 返回格式示例::

            [
              {
                "domain": "light",
                "services": {
                  "turn_on": {
                    "fields": {
                      "brightness": {...},
                      "color_temp": {...},
                      ...
                    }
                  },
                  ...
                }
              },
              ...
            ]
        """
        client = await self._get_client()
        response = await client.get("/api/services", timeout=30.0)
        response.raise_for_status()
        return response.json()
