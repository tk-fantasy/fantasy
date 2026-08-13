from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..clients.ha_client import HomeAssistantClient
from ..core.config import get_config

logger = logging.getLogger(__name__)

class HAService:
    """Home Assistant 设备管理服务。"""

    _STATES_CACHE_TTL = 5.0

    def __init__(self, client: HomeAssistantClient | None = None) -> None:
        self._client = client or HomeAssistantClient()
        self._states_cache: list[dict[str, Any]] | None = None
        self._states_cache_at: float = 0.0
        # registry 缓存（area/entity/device，60s TTL，一次 WS 拉取）
        self._area_map: dict[str, str] = {}            # area_id -> area_name
        self._entity_area_map: dict[str, str] = {}     # entity_id -> area_id（含 device 继承）
        self._device_info_map: dict[str, dict] = {}    # device_id -> {name,model,...}
        self._entity_device_map: dict[str, str] = {}   # entity_id -> device_id
        self._registry_cache_at: float = 0.0
        # 用户自定义实体别名缓存（DB 读取，30s TTL）：entity_id -> alias
        self._alias_map: dict[str, str] = {}
        self._alias_cache_at: float = 0.0

    def invalidate_states_cache(self) -> None:
        """使状态缓存失效。调用服务控制设备后调用，确保下次拉取拿到 HA 最新状态。"""
        self._states_cache = None
        self._states_cache_at = 0.0
        # 实体改名会同时影响 registry（device/entity）和别名，一并失效让下次读取拿到最新
        self._registry_cache_at = 0.0
        self._alias_cache_at = 0.0

    async def _get_alias_map(self) -> dict[str, str]:
        """读取用户自定义实体别名（DB，30s TTL）。返回 {entity_id: alias}。"""
        now = time.time()
        if now - self._alias_cache_at < 30.0 and self._alias_map:
            return self._alias_map
        try:
            from ..core.database import Database
            db = Database.get()
            self._alias_map = await db.prefs_get_by_scope("entity_alias")
            self._alias_cache_at = now
        except Exception:
            logger.debug("读取实体别名失败（DB 未就绪？）", exc_info=True)
        return self._alias_map

    async def _get_states_cached(self) -> list[dict[str, Any]]:
        """获取所有设备状态（带缓存，TTL 5秒）。"""
        now = time.time()
        if self._states_cache is not None and (now - self._states_cache_at) < self._STATES_CACHE_TTL:
            return self._states_cache
        states = await self._client.get_states()
        self._states_cache = states
        self._states_cache_at = now
        return states

    async def get_states_snapshot(self) -> list[dict[str, Any]]:
        """所有实体状态快照（带 5s 缓存）。供设备状态门控等只读路径复用，单次评估
        周期内多规则共享一次 HA 拉取（命中缓存 0 网络开销）。"""
        return await self._get_states_cached()

    async def _refresh_registry(self) -> None:
        """一次 WS 拉取 area/device/entity registry，填充四个缓存 map（60s TTL）。

        entity 的 area_id 优先取实体自身配置；为空时继承其所属 device 的 area_id。
        HA 推荐做法是给 device 分配区域，此时实体自身 area_id 为空、靠 device 继承，
        若不做继承会把整批设备（如 Xiaomi Home）错误地当成「未分配区域」丢弃。
        """
        now = time.time()
        if now - self._registry_cache_at < 60.0:
            return
        try:
            import json
            import websockets
            ws_url = self._client.base_url.replace("http", "ws") + "/api/websocket"
            headers = {}
            token = self._client.token
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with asyncio.timeout(5):  # 5 秒超时
                async with websockets.connect(ws_url, additional_headers=headers) as ws:
                    await ws.recv()
                    await ws.send(json.dumps({"type": "auth", "access_token": token}))
                    auth_result = json.loads(await ws.recv())
                    if auth_result.get("type") != "auth_ok":
                        raise RuntimeError(f"HA WebSocket auth failed: {auth_result}")
                    # 串行递增 id，避免 HA WebSocket「id 已使用」报错
                    msg_id = 0

                    async def call(msg_type: str) -> dict[str, Any]:
                        nonlocal msg_id
                        msg_id += 1
                        await ws.send(json.dumps({"id": msg_id, "type": msg_type}))
                        return json.loads(await ws.recv())

                    # areas
                    areas = (await call("config/area_registry/list")).get("result", [])
                    self._area_map = {a["area_id"]: a["name"] for a in areas}
                    # device registry
                    devices = (await call("config/device_registry/list")).get("result", [])
                    self._device_info_map = {
                        dv["id"]: {
                            "name": dv.get("name_by_user") or dv.get("name") or "",
                            "model": dv.get("model"),
                            "manufacturer": dv.get("manufacturer"),
                            "sw_version": dv.get("sw_version"),
                            "area_id": dv.get("area_id"),
                        }
                        for dv in devices
                    }
                    device_area = {
                        did: info["area_id"]
                        for did, info in self._device_info_map.items()
                        if info.get("area_id")
                    }
                    # entity registry
                    registry = (await call("config/entity_registry/list")).get("result", [])
                    self._entity_device_map = {
                        e["entity_id"]: e.get("device_id")
                        for e in registry
                        if e.get("device_id")
                    }
                    # entity 自身 area 优先，为空则继承所属 device 的 area
                    self._entity_area_map = {
                        e["entity_id"]: (
                            e.get("area_id")
                            or device_area.get(e.get("device_id", ""), "")
                        )
                        for e in registry
                        if (e.get("area_id") or device_area.get(e.get("device_id", "")))
                    }
            self._registry_cache_at = now
            logger.debug(
                "registry 缓存：areas=%d devices=%d entity_area=%d",
                len(self._area_map), len(self._device_info_map),
                len(self._entity_area_map))
        except asyncio.TimeoutError:
            logger.warning("获取 HA registry 超时")
        except Exception:
            logger.warning("获取 HA registry 失败", exc_info=True)

    async def _get_area_maps_cached(self) -> tuple[dict[str, str], dict[str, str]]:
        """获取 area_id→area_name 和 entity_id→area_id 映射（缓存 60 秒）。

        保留旧签名，供 get_all_devices 等既有消费者使用。
        """
        await self._refresh_registry()
        return self._area_map, self._entity_area_map

    async def _get_full_registry(self) -> dict[str, Any]:
        """返回完整 registry bundle，供设备分组等方法使用。"""
        await self._refresh_registry()
        return {
            "area_map": self._area_map,
            "entity_area_map": self._entity_area_map,
            "device_info_map": self._device_info_map,
            "entity_device_map": self._entity_device_map,
        }

    async def get_entity_name_map(self) -> dict[str, str]:
        """返回 {entity_id: friendly_name} 映射，复用 states 缓存（5s TTL）。

        供 dispatcher 在发 CallTool 事件时把 entity_id 翻译成友好名。
        """
        states = await self._get_states_cached()
        return {
            s["entity_id"]: s["attributes"].get("friendly_name", s["entity_id"])
            for s in states
        }

    async def get_areas(self) -> list[dict[str, str]]:
        """对外暴露 HA 区域列表(供摄像头管理页区域下拉)。

        复用 _get_area_maps_cached 的 area_id→name 映射。
        """
        area_map, _ = await self._get_area_maps_cached()
        return [{"area_id": aid, "name": name} for aid, name in area_map.items()]

    # 可控/可展示的设备 domain — 过滤掉 sun/zone/person/update 等 HA 内置实体
    _DEVICE_DOMAINS = frozenset({
        "light", "switch", "climate", "cover", "fan", "humidifier",
        "sensor", "binary_sensor", "lock", "media_player", "vacuum",
        "valve", "water_heater", "siren", "alarm_control_panel",
    })

    def _virtual_suppress_set(self, states_by_id: dict[str, dict]) -> set[str]:
        """返回应隐藏的模拟器实体集合。

        规则：配置白名单(simulator.entity_ids)中当前存在的实体若【全部】
        unavailable/unknown → 返回全部；否则返回空集。
        匹配「全部离线才隐藏」语义。白名单为空则特性关闭（kill switch）。
        """
        whitelist = set(get_config("simulator.entity_ids", []) or [])
        if not whitelist:
            return set()
        present = [eid for eid in whitelist if eid in states_by_id]
        if present and all(
            states_by_id[eid].get("state") in ("unavailable", "unknown")
            for eid in present
        ):
            return set(present)
        return set()

    async def get_all_devices(self) -> list[dict[str, Any]]:
        """获取所有设备（含区域信息）。

        双重过滤：domain 白名单（排除 sun/zone/person 等内置实体）
        + area_id 非空（只显示已分配区域的设备，未分配的不显示）。
        """
        states = await self._get_states_cached()
        states_by_id = {s["entity_id"]: s for s in states}
        suppress = self._virtual_suppress_set(states_by_id)
        area_map, entity_area_map = await self._get_area_maps_cached()
        alias_map = await self._get_alias_map()
        devices = []
        for state in states:
            entity_id = state["entity_id"]
            if entity_id in suppress:
                continue
            domain = entity_id.split(".")[0]
            if domain not in self._DEVICE_DOMAINS:
                continue
            area_id = entity_area_map.get(entity_id)
            if area_id is None:
                continue
            devices.append({
                "entity_id": entity_id,
                "domain": domain,
                # 用户自定义别名优先，否则用 HA 生成的 friendly_name
                "name": alias_map.get(entity_id) or state["attributes"].get("friendly_name", entity_id),
                "state": state["state"],
                "attributes": state["attributes"],
                "area_id": area_id,
                "area_name": area_map.get(area_id) if area_id else None,
            })
        return devices

    async def get_all_devices_grouped(self) -> dict[str, Any]:
        """按物理设备聚合的设备视图：区域 → 物理设备 → 子实体。

        与 get_all_devices 用相同的白名单+area 过滤，但额外：
        - 把实体按 device_id 聚合成物理设备组
        - 无 device_id 的实体（如 MQTT 手动配置）生成虚拟设备
        - 每个 device 携带 model/manufacturer/sw_version 等元信息

        返回结构见 docs/.../device-grouped-entity-display-design.md。
        """
        states = await self._get_states_cached()
        reg = await self._get_full_registry()
        area_map = reg["area_map"]
        device_info_map = reg["device_info_map"]
        entity_device_map = reg["entity_device_map"]
        alias_map = await self._get_alias_map()

        # 先把白名单+area 过滤后的实体收集成 {entity_id: state_dict}
        by_id: dict[str, dict] = {}
        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            if domain not in self._DEVICE_DOMAINS:
                continue
            # area 过滤（entity 自身或 device 继承）
            eid_area = self._entity_area_map.get(entity_id)
            if not eid_area:
                continue
            by_id[entity_id] = state

        # 按 device_id 聚合；无 device_id 的实体各成一个虚拟设备
        groups: dict[str, list[str]] = {}
        for entity_id in by_id:
            did = entity_device_map.get(entity_id) or f"virtual:{entity_id}"
            groups.setdefault(did, []).append(entity_id)

        devices_out: list[dict[str, Any]] = []
        for did, entity_ids in groups.items():
            first_state = by_id[entity_ids[0]]
            if did.startswith("virtual:"):
                # 无 device 维度：用实体自身信息填充
                dev_info = {
                    "name": first_state["attributes"].get("friendly_name", entity_ids[0]),
                    "model": None,
                    "manufacturer": None,
                    "sw_version": None,
                }
            else:
                dev_info = device_info_map.get(did, {})
                if not dev_info.get("name"):
                    dev_info = {**dev_info,
                                "name": first_state["attributes"].get("friendly_name", did)}

            # area：device 上的实体可能来自不同 area，取第一个实体的 area
            sample_area = self._entity_area_map.get(entity_ids[0], "")
            entities_list = []
            for entity_id in sorted(entity_ids):
                state = by_id[entity_id]
                entities_list.append({
                    "entity_id": entity_id,
                    "domain": entity_id.split(".")[0],
                    # 用户自定义别名优先
                    "name": alias_map.get(entity_id) or state["attributes"].get("friendly_name", entity_id),
                    "state": state["state"],
                    "attributes": state["attributes"],
                })
            # 主控实体（controllable）：用于向用户描述设备能力的简短摘要。
            # 判断依据：domain 在白名单且该 domain 通常可控（light/switch/media_player 等）。
            # 诊断/配置类（sensor 故障值、版本号、音频ID 等）不计入摘要，避免 AI 念出噪音。
            controllable = [
                e for e in entities_list
                if e["domain"] in {
                    "light", "switch", "climate", "cover", "fan", "humidifier",
                    "lock", "media_player", "vacuum", "valve", "water_heater",
                    "siren", "alarm_control_panel",
                }
            ]
            # summary：用 domain 中文描述能力，不用子实体的 friendly_name
            # （后者常带 MIoT 注入的噪声，如「麦克风 静音」「睡眠模式」，会让
            # AI 把同一设备的子功能当独立设备念给用户）。单一可控实体时直接用设备名。
            if len(controllable) == 1:
                summary_text = dev_info.get("name") or entity_ids[0]
            elif len(controllable) > 1:
                summary_text = f"{len(controllable)}个可控功能"
            else:
                # 纯诊断设备（如网关）：不拼 sensor friendly_name（常有 JSON 噪声）
                summary_text = "属性查看"
            devices_out.append({
                "device_id": did,
                "name": dev_info.get("name") or entity_ids[0],
                "model": dev_info.get("model"),
                "manufacturer": dev_info.get("manufacturer"),
                "sw_version": dev_info.get("sw_version"),
                "area_id": sample_area,
                "area_name": area_map.get(sample_area) if sample_area else None,
                "entity_count": len(entities_list),
                "controllable_count": len(controllable),
                # summary 帮助 LLM 用一句话描述设备，无需逐个解析子实体
                "summary": summary_text,
                "entities": entities_list,
            })
        # 稳定排序：按区域名 → 设备名
        devices_out.sort(key=lambda d: (d.get("area_name") or "", d.get("name") or ""))
        return {"devices": devices_out}

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
