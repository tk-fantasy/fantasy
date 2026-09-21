"""场景模式 — 一句话/一键切换一组设备到预设状态（"回家""观影""睡眠"）。

纯核心功能（scenes 表 + HA 调用），零插件依赖。聊天工具与 REST 共用本服务
（tools.py 注册 scene_* 工具，scene_routes.py 提供 REST）。

actions 格式（与 HA service 调用一致，capture 从当前状态生成同构数据）：
    [{"domain": "light", "service": "turn_on", "entity_id": "light.ke_ting",
      "data": {"brightness": 200}}, ...]

HA Scenes 复用（阶段3）：HA 原生场景实体（domain=scene）直接列出/应用/捕获，
不镜像进本地 scenes 表——HA 侧创建/编辑/管理的场景在本系统即取即用，
本地表只承载 Aether 自建场景，两池并存、HA 优先解析。
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
import uuid
from typing import Any

from ..core.database import Database

logger = logging.getLogger(__name__)

# capture 覆盖的 domain（MVP：最常用的家庭设备类型）
_CAPTURABLE_DOMAINS = {"light", "switch", "cover", "fan", "climate", "humidifier"}


class SceneService:
    def __init__(self, ha_client: Any = None, ha_service: Any = None) -> None:
        # list[0] ref 模式：运行时热替换（与 ha_client_ref 同模式）
        self._ha_client_ref: list = [ha_client]
        self._ha_service_ref: list = [ha_service]

    def set_ha(self, ha_client: Any, ha_service: Any) -> None:
        self._ha_client_ref[0] = ha_client
        self._ha_service_ref[0] = ha_service

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def list_scenes(self) -> list[dict]:
        return await Database.get().scenes_all()

    async def get_scene(self, scene_id: str) -> dict | None:
        return await Database.get().scenes_get(scene_id)

    async def create_scene(self, name: str, actions: list[dict], user_id: str = "",
                           scene_id: str = "") -> dict:
        """新建/更新场景。actions 为空直接拒绝。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("场景名不能为空")
        actions = self._sanitize_actions(actions)
        if not actions:
            raise ValueError("场景至少需要一个动作")
        scene_id = scene_id or str(uuid.uuid4())[:8]
        await Database.get().scenes_upsert(scene_id, name, actions, user_id)
        logger.info("Scene saved: '%s' (%s, %d actions)", name, scene_id, len(actions))
        return await self.get_scene(scene_id) or {"id": scene_id, "name": name, "actions": actions}

    async def delete_scene(self, scene_id: str) -> bool:
        return await Database.get().scenes_delete(scene_id)

    # ------------------------------------------------------------------
    # 应用
    # ------------------------------------------------------------------

    async def apply_scene(self, scene_id: str) -> dict:
        """逐条应用场景动作（单条失败继续其余，结果逐条返回）。"""
        scene = await self.get_scene(scene_id)
        if scene is None:
            raise ValueError(f"场景不存在: {scene_id}")
        ha = self._ha_client_ref[0]
        if ha is None:
            raise RuntimeError("HA 服务不可用")
        results = []
        for action in scene["actions"]:
            domain = action.get("domain", "")
            service = action.get("service", "")
            entity_id = action.get("entity_id", "")
            data = action.get("data") or {}
            try:
                await ha.call_service(domain, service, entity_id, data)
                results.append({"entity_id": entity_id, "ok": True})
            except Exception as exc:  # noqa: BLE001
                logger.warning("Scene '%s' action failed: %s/%s %s: %s",
                               scene.get("name"), domain, service, entity_id, exc)
                results.append({"entity_id": entity_id, "ok": False, "error": str(exc)})
        ok_count = sum(1 for r in results if r.get("ok"))
        logger.info("Scene '%s' applied: %d/%d ok", scene.get("name"), ok_count, len(results))
        return {"scene": scene.get("name"), "total": len(results), "ok": ok_count, "results": results}

    # ------------------------------------------------------------------
    # 从当前状态捕获
    # ------------------------------------------------------------------

    async def capture_scene(self, name: str, user_id: str = "") -> dict:
        """把当前所有可控设备的状态存成一个场景（"现在这样就是我要的观影模式"）。"""
        ha = self._ha_service_ref[0]
        if ha is None:
            raise RuntimeError("HA 服务不可用")
        entities = await ha.get_all_devices()
        actions: list[dict] = []
        for ent in entities:
            entity_id = str(ent.get("entity_id", "") or ent.get("id", ""))
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            if domain not in _CAPTURABLE_DOMAINS:
                continue
            data = self._current_state_to_action_data(ent)
            if data is None:
                continue
            service, params = data
            actions.append({
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "data": params,
            })
        if not actions:
            raise ValueError("没有可捕获的设备状态（检查 HA 连接）")
        return await self.create_scene(name, actions, user_id=user_id)

    @staticmethod
    def _current_state_to_action_data(ent: dict) -> tuple[str, dict] | None:
        """从实体当前状态推导 (service, data)。不可推导的返回 None 跳过。"""
        state = str(ent.get("state", "") or ent.get("status", ""))
        attrs = ent.get("attributes", {}) or {}
        domain = str(ent.get("entity_id", ent.get("id", ""))).split(".")[0]
        if domain in ("light", "switch", "fan", "humidifier"):
            service = "turn_on" if state == "on" else "turn_off"
            params: dict = {}
            if service == "turn_on" and domain == "light":
                # 保留亮度/色温（有才带）
                if attrs.get("brightness") is not None:
                    params["brightness"] = int(attrs["brightness"])
                if attrs.get("color_temp") is not None:
                    params["color_temp_kelvin"] = attrs["color_temp"]
            return service, params
        if domain == "cover":
            if state == "open":
                return "set_cover_position", {"position": int(attrs.get("current_position", 100) or 100)}
            return "close_cover", {}
        if domain == "climate":
            service = "turn_on" if state != "off" else "turn_off"
            params = {}
            temp = attrs.get("temperature")
            if temp is not None:
                params["temperature"] = temp
            return service, params
        return None

    @staticmethod
    def _sanitize_actions(actions: list) -> list[dict]:
        """只保留结构合法的动作条目，字段白名单化。"""
        clean = []
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            domain = str(a.get("domain", "")).strip()
            service = str(a.get("service", "")).strip()
            entity_id = str(a.get("entity_id", "")).strip()
            if not (domain and service and entity_id and "." in entity_id):
                continue
            data = a.get("data") if isinstance(a.get("data"), dict) else {}
            clean.append({"domain": domain, "service": service,
                          "entity_id": entity_id, "data": data})
        return clean

    # ------------------------------------------------------------------
    # HA Scenes 复用（阶段3）
    # ------------------------------------------------------------------

    async def list_ha_scenes(self) -> list[dict]:
        """HA 原生场景实体（domain=scene）。HA 不可用/未配置返回空列表。"""
        ha = self._ha_service_ref[0]
        if ha is None:
            return []
        try:
            entities = await ha.get_entities_by_domains({"scene"})
        except Exception:
            logger.warning("list HA scenes failed", exc_info=True)
            return []
        return [{"entity_id": e["entity_id"], "name": e["name"],
                 "state": e["state"], "kind": "ha"} for e in entities]

    async def apply_ha_scene(self, entity_id: str) -> dict:
        """应用 HA 原生场景（scene.turn_on，一条原子指令整组切换）。"""
        ha = self._ha_client_ref[0]
        if ha is None:
            raise RuntimeError("HA 服务不可用")
        entity_id = (entity_id or "").strip()
        if not entity_id.startswith("scene."):
            raise ValueError(f"不是 HA 场景实体: {entity_id}")
        await ha.call_service("scene", "turn_on", entity_id)
        name = entity_id.split(".", 1)[1]
        logger.info("HA scene applied: %s", entity_id)
        return {"scene": name, "via": "ha", "ok": 1, "total": 1, "results": []}

    async def capture_ha_scene(self, name: str) -> dict:
        """用 HA scene.create 从当前状态生成 HA 原生场景。

        生成的场景落在 HA 侧（.storage，可在 HA 界面管理/编辑），本系统
        不镜像——列场景时实时拉取即见。中文名转 ASCII slug 做 scene_id，
        显示名保留中文。
        """
        ha = self._ha_client_ref[0]
        if ha is None:
            raise RuntimeError("HA 服务不可用")
        name = (name or "").strip()
        if not name:
            raise ValueError("场景名不能为空")
        ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9_]+", "_", ascii_name.lower()).strip("_")
        scene_id = slug or f"aether_{int(time.time())}"
        await ha.call_service(
            "scene", "create", data={"scene_id": scene_id, "name": name})
        logger.info("HA scene captured: '%s' -> scene.%s", name, scene_id)
        return {"id": f"scene.{scene_id}", "name": name, "kind": "ha"}

    async def resolve_scene(self, *, scene_id: str = "", name: str = "") -> dict | None:
        """跨两个场景池解析：HA 原生场景优先，Aether 本地场景兜底。

        返回 {"kind": "ha"|"local", "id": entity_id 或本地 id, "name": 显示名}；
        解析不到返回 None。名字精确匹配（与既有聊天工具语义一致，模糊
        匹配交给 LLM 看 scene_list 候选自己做）。
        """
        if scene_id:
            if scene_id.startswith("scene."):
                return {"kind": "ha", "id": scene_id,
                        "name": scene_id.split(".", 1)[1]}
            local = await self.get_scene(scene_id)
            if local is not None:
                return {"kind": "local", "id": local["id"], "name": local["name"]}
            return None
        if not name:
            return None
        for e in await self.list_ha_scenes():
            if e["name"] == name or e["entity_id"] == f"scene.{name}":
                return {"kind": "ha", "id": e["entity_id"], "name": e["name"]}
        for s in await self.list_scenes():
            if s["name"] == name:
                return {"kind": "local", "id": s["id"], "name": s["name"]}
        return None
