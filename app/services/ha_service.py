from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..clients.ha_client import HomeAssistantClient

logger = logging.getLogger(__name__)

class HAService:
    """Home Assistant 设备管理服务。"""

    _STATES_CACHE_TTL = 5.0

    def __init__(self, client: HomeAssistantClient | None = None) -> None:
        self._client = client or HomeAssistantClient()
        self._states_cache: list[dict[str, Any]] | None = None
        self._states_cache_at: float = 0.0
        # area 缓存 {area_id: area_name} 和 entity→area 映射
        self._area_map: dict[str, str] = {}
        self._entity_area_map: dict[str, str] = {}
        self._area_cache_at: float = 0.0

    async def _get_states_cached(self) -> list[dict[str, Any]]:
        """获取所有设备状态（带缓存，TTL 5秒）。"""
        now = time.time()
        if self._states_cache is not None and (now - self._states_cache_at) < self._STATES_CACHE_TTL:
            return self._states_cache
        states = await self._client.get_states()
        self._states_cache = states
        self._states_cache_at = now
        return states

    async def _get_area_maps_cached(self) -> tuple[dict[str, str], dict[str, str]]:
        """获取 area_id→area_name 和 entity_id→area_id 映射（缓存 60 秒）。"""
        now = time.time()
        if now - self._area_cache_at < 60.0:
            return self._area_map, self._entity_area_map
        try:
            # 通过 WebSocket 连接 HA，获取 areas 和 entity registry
            import json
            import websockets
            ws_url = self._client._base_url.replace("http", "ws") + "/api/websocket"
            headers = {}
            if self._client._token:
                headers["Authorization"] = f"Bearer {self._client._token}"
            async with asyncio.timeout(5):  # 5 秒超时
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"type": "auth", "access_token": self._client._token}))
                    auth_result = json.loads(await ws.recv())
                    if auth_result.get("type") != "auth_ok":
                        raise RuntimeError(f"HA WebSocket auth failed: {auth_result}")
                    # 获取 areas
                    await ws.send(json.dumps({"id": 1, "type": "config/area_registry/list"}))
                    areas_resp = json.loads(await ws.recv())
                    areas = areas_resp.get("result", [])
                    self._area_map = {a["area_id"]: a["name"] for a in areas}
                    # 获取 entity registry
                    await ws.send(json.dumps({"id": 2, "type": "config/entity_registry/list"}))
                    reg_resp = json.loads(await ws.recv())
                    registry = reg_resp.get("result", [])
                    self._entity_area_map = {
                        e["entity_id"]: e["area_id"]
                        for e in registry
                        if e.get("area_id")
                    }
            self._area_cache_at = now
            logger.debug("获取到 %d 个区域、%d 个实体映射 - 已缓存", len(self._area_map), len(self._entity_area_map))
        except asyncio.TimeoutError:
            logger.warning("获取 HA area/entity registry 超时")
        except Exception:
            logger.warning("获取 HA area/entity registry 失败", exc_info=True)
        return self._area_map, self._entity_area_map

    async def get_entity_name_map(self) -> dict[str, str]:
        """返回 {entity_id: friendly_name} 映射，复用 states 缓存（5s TTL）。

        供 dispatcher 在发 CallTool 事件时把 entity_id 翻译成友好名。
        """
        states = await self._get_states_cached()
        return {
            s["entity_id"]: s["attributes"].get("friendly_name", s["entity_id"])
            for s in states
        }

    async def get_all_devices(self) -> list[dict[str, Any]]:
        """获取所有设备（含区域信息）。"""
        states = await self._get_states_cached()
        area_map, entity_area_map = await self._get_area_maps_cached()
        devices = []
        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            area_id = entity_area_map.get(entity_id)
            if area_id is None:
                continue
            devices.append({
                "entity_id": entity_id,
                "domain": domain,
                "name": state["attributes"].get("friendly_name", entity_id),
                "state": state["state"],
                "attributes": state["attributes"],
                "area_id": area_id,
                "area_name": area_map.get(area_id) if area_id else None,
            })
        return devices

    @staticmethod
    async def get_service_defs(
        ha_client,
        *,
        domains: set[str] | None = None,
        include_required: bool = False,
    ) -> dict[str, dict]:
        """获取 HA 服务定义。

        Args:
            ha_client: Home Assistant HTTP 客户端实例。
            domains: 可选，只保留这些 domain 的服务。None 表示不过滤。
            include_required: 是否在返回中包含 required 字段列表。

        返回 {domain: {svc_name: {"fields": [...], "required": [...]}}}。
        """
        services_info: dict[str, dict] = {}
        try:
            for svc_entry in await ha_client.get_services():
                domain = svc_entry.get("domain", "")
                if domains is not None and domain not in domains:
                    continue
                services = {}
                for svc_name, svc_def in svc_entry.get("services", {}).items():
                    fields_dict = svc_def.get("fields", {})
                    entry = {"fields": list(fields_dict.keys())}
                    if include_required:
                        entry["required"] = [
                            fname
                            for fname, fdef in fields_dict.items()
                            if fdef.get("required", False)
                        ]
                    services[svc_name] = entry
                services_info[domain] = services
        except Exception:
            logger.warning("Failed to get HA service definitions", exc_info=True)
        return services_info
