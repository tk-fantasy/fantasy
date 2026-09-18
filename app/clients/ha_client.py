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


class HomeAssistantClient:
    """Home Assistant REST API 客户端。"""

    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self._base_url = (base_url or get_config("ha.url") or DEFAULT_HA_URL).rstrip("/")
        self._token = token or get_config("ha.token") or ""
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

    @property
    def base_url(self) -> str:
        """HA 服务地址（已 rstrip / ），供外部服务读，避免访问 _base_url 私有属性。"""
        return self._base_url

    @property
    def token(self) -> str:
        """Long-Lived Access Token，供外部服务读，避免访问 _token 私有属性。"""
        return self._token

    # ============ 状态查询 ============

    async def get_states(self) -> list[dict[str, Any]]:
        """获取所有实体状态。"""
        client = await self._get_client()
        response = await client.get("/api/states")
        response.raise_for_status()
        return response.json()

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """获取单个实体状态；不存在返回 None（HA 对未知 entity_id 返回 404）。

        call_service 回读等只需一个实体的场景用本方法，避免全量 /api/states。
        """
        client = await self._get_client()
        response = await client.get(f"/api/states/{entity_id}")
        if response.status_code in (404, 405):
            return None
        response.raise_for_status()
        return response.json()

    async def ping(self) -> bool:
        """轻量探活：GET /api/（约百字节），替代全量 /api/states 的健康检查。

        稳态下健康探活按分钟级轮询，用全量 states 探活等于每分钟白拉 1-2MB。
        """
        client = await self._get_client()
        response = await client.get("/api/")
        response.raise_for_status()
        return response.status_code == 200

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

    # ============ 历史数据 ============

    async def get_history(
        self,
        filter_entity_id: str | None = None,
        timestamp: str | None = None,
        end_time: str | None = None,
        minimal: bool | None = None,
    ) -> list[list[dict[str, Any]]]:
        """查询 HA 历史状态记录。

        HA API: GET /api/history/period/<timestamp>?filter_entity_id=...&end_time=...

        Args:
            filter_entity_id: 实体 ID（逗号分隔多个），None 则返回全部实体
            timestamp: 起始时间 ISO8601，None 表示从最早开始
            end_time: 结束时间 ISO8601，None 表示到现在
            minimal: True 时仅返回最少字段（state/last_changed）

        Returns:
            外层列表每项对应一个实体，内层列表是该实体的历史状态点。
        """
        client = await self._get_client()
        path = "/api/history/period" + (f"/{timestamp}" if timestamp else "")
        params: dict[str, Any] = {}
        if filter_entity_id is not None:
            params["filter_entity_id"] = filter_entity_id
        if end_time is not None:
            params["end_time"] = end_time
        if minimal is not None:
            params["minimal"] = minimal
        response = await client.get(path, params=params or None, timeout=30.0)
        response.raise_for_status()
        return response.json()

    # ============ 摄像头 ============

    async def camera_proxy(self, entity_id: str) -> bytes:
        """抓取 HA 摄像头当前帧（JPEG bytes）。

        GET /api/camera_proxy/{entity_id}：HA 侧生成快照，单帧分析一张 JPEG
        就够，Aether 不再需要为触发/推理维护常驻解码器。部分集成会 302 到
        带签名 token 的临时 URL，按请求级 follow_redirects 跟随（共享 client
        默认不跟随，不影响其他调用）。
        """
        client = await self._get_client()
        response = await client.get(
            f"/api/camera_proxy/{entity_id}", timeout=15.0, follow_redirects=True)
        response.raise_for_status()
        return response.content

    # ============ HA 原生自动化配置（阶段5；适配 HA 2026.x）============

    async def list_automations(self) -> list[dict[str, Any]]:
        """列出 HA 原生自动化。

        HA 2026 起没有「列出全部 config」的端点（WS config/automation/config/*
        命令已移除），automations 本身就是实体——从 /api/states 按 domain=automation
        取，friendly_name=alias、attributes.id 是 config id（删除用）。
        """
        client = await self._get_client()
        response = await client.get("/api/states", timeout=15.0)
        response.raise_for_status()
        out = []
        for s in response.json():
            entity_id = str(s.get("entity_id", "") or "")
            if not entity_id.startswith("automation."):
                continue
            attrs = s.get("attributes") or {}
            out.append({
                "id": str(attrs.get("id") or entity_id.split(".", 1)[1]),
                "entity_id": entity_id,
                "alias": str(attrs.get("friendly_name") or entity_id),
                "state": s.get("state"),
                "last_triggered": attrs.get("last_triggered"),
            })
        return out

    async def create_automation(
        self, config: dict[str, Any], automation_id: str = "",
    ) -> dict[str, Any]:
        """创建 HA 原生自动化。

        REST 只接受路径带显式 id（POST /api/config/automation/config/{id}，
        HA 2026 起无免 id 创建端点），缺省生成 uuid hex。返回 {id, result}。
        """
        import uuid as _uuid
        automation_id = automation_id or _uuid.uuid4().hex
        client = await self._get_client()
        response = await client.post(
            f"/api/config/automation/config/{automation_id}",
            json=config, timeout=15.0)
        if response.status_code >= 400:
            # 带 HA 响应体抛错：LLM 编译的字段被 HA 拒时（400），只有状态码
            # 无法归因（httpx 报文不含 HA 说的事实原因）
            try:
                detail = str(response.json())[:200]
            except Exception:  # noqa: BLE001
                detail = (response.text or "")[:200]
            raise RuntimeError(f"HA {response.status_code}: {detail}")
        return {"id": automation_id, "result": (response.json() or {}).get("result", "")}

    async def delete_automation(self, automation_id: str) -> None:
        """删除 HA 原生自动化（按 config id）。不存在时 HA 返回 404。"""
        client = await self._get_client()
        response = await client.delete(
            f"/api/config/automation/config/{automation_id}", timeout=15.0)
        response.raise_for_status()

    async def remove_entity(self, entity_id: str) -> bool:
        """从 entity_registry 移除实体（WS config/entity_registry/remove）。

        HA 2026 实测：删除 automation config 后实体可能残留为 unavailable
        幽灵（state 停留 unavailable、config API 已 404），需要注册表层面移除。
        一次性短连接 WS，握手模板同 update_entity_name。
        """
        import json as _json
        import websockets
        scheme, _, rest = self._base_url.partition("://")
        ws_url = f"{'wss' if scheme == 'https' else 'ws'}://{rest}/api/websocket"
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with asyncio.timeout(5):
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    await ws.recv()
                    await ws.send(_json.dumps({"type": "auth", "access_token": self._token}))
                    auth = _json.loads(await ws.recv())
                    if auth.get("type") != "auth_ok":
                        raise RuntimeError(f"HA auth failed: {auth}")
                    await ws.send(_json.dumps({
                        "id": 1, "type": "config/entity_registry/remove",
                        "entity_id": entity_id,
                    }))
                    resp = _json.loads(await ws.recv())
                    if resp.get("id") != 1:
                        raise RuntimeError("entity_registry/remove: unexpected frame")
                    return bool(resp.get("success"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("remove_entity(%s) failed: %s", entity_id, exc)
            return False

    # ============ 日历 ============

    async def logbook(
        self, entity_id: str | None = None,
        timestamp: str | None = None, end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询 HA logbook（设备级操作史：谁在何时开了灯/关了门）。

        HA 2026 起按实体的路径形式（/api/logbook/{entity_id}）已失效，统一
        根路径 + entity_id 查询参数过滤。timestamp/end_time 为 ISO8601。
        返回 [{when, name, entity_id, message, context_user_id?}, ...]，
        时间升序——Aether 自家的 family_events 只有自家操作，HA 侧的
        App/自动化/家庭成员操作史都在这里。
        """
        client = await self._get_client()
        params: dict[str, str] = {}
        if entity_id:
            params["entity_id"] = entity_id
        if timestamp:
            params["timestamp"] = timestamp
        if end_time:
            params["end_time"] = end_time
        response = await client.get(
            "/api/logbook", params=params or None, timeout=15.0)
        response.raise_for_status()
        return response.json()

    async def calendar_events(
        self, entity_id: str, start: str, end: str,
    ) -> list[dict[str, Any]]:
        """查询日历实体在 [start, end) 内的日程（ISO8601）。

        返回 [{summary, start: {dateTime}, end: {dateTime}, description?, uid?}]。
        calendar 实体的 state_changed 不可靠（多数集成 state 长期 off、事件藏在
        attribute），日程对齐只能轮询本接口（EventTriggerService 每 5 分钟）。
        """
        client = await self._get_client()
        response = await client.get(
            f"/api/calendars/{entity_id}",
            params={"start": start, "end": end}, timeout=10.0)
        response.raise_for_status()
        return response.json()

    # ============ 实体注册表写操作 ============

    async def update_entity_name(
        self, entity_id: str, name: str | None
    ) -> dict[str, Any]:
        """更新 HA entity_registry 里的 name（同步到 HA 原生）。

        name=None 表示清除自定义名，恢复集成生成的默认 friendly_name。
        通过 WebSocket 调 config/entity_registry/update。
        """
        import json
        import websockets

        # 只替换协议头（http→ws / https→wss）：全量 replace 会把主机名里
        # 含 "http" 的地址（如 http://http-proxy.lan）一并改坏
        scheme, _, rest = self._base_url.partition("://")
        ws_url = f"{'wss' if scheme == 'https' else 'ws'}://{rest}/api/websocket"
        headers = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        async with asyncio.timeout(5):
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = json.loads(await ws.recv())
                if auth.get("type") != "auth_ok":
                    raise RuntimeError(f"HA auth failed: {auth}")
                await ws.send(json.dumps({
                    "id": 1,
                    "type": "config/entity_registry/update",
                    "entity_id": entity_id,
                    "name": name,
                }))
                resp = json.loads(await ws.recv())
                # 校验帧配对：HA 可能穿插其他消息，拿错帧时 success 缺失
                # 会误报"改名失败"（实际成功）
                if resp.get("id") != 1:
                    raise RuntimeError("HA entity_registry/update: unexpected frame")
                if not resp.get("success"):
                    err = resp.get("error", {})
                    raise RuntimeError(
                        f"HA entity_registry/update failed: {err.get('message', resp)}")
                return resp.get("result", {})
