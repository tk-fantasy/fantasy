"""统一设备注册表 — AI 视图与闸门候选的唯一快照源。

此前模型看的（catalog/controls/get_entities 三套文本各自拼）与闸门校验用的
（实时 states + 黑名单 + match_devices 扁平列表）是平行数据源，出现「闸门认识、
视图不认识」的漂移：子实体名「A灯 会客厅灯 左键」在 match_devices 里能命中，
但三套视图都刻意隐藏了它，模型只能编造 light.hall。

本模块把数据源收敛为一处：build_device_snapshot() 一次聚合 grouped 实体 +
controls + DB 三 scope（entity_note / entity_operable / entity_alias）+ 语义
翻转 state，所有视图渲染与候选反查从这里出。

边界（重要）：这是 ≤60s 的缓存快照（刷新策略由消费方决定），不能替代
call_service 的实时存在性校验——那是唯一硬边界，数据源必须是实时 HA states。
快照只负责「给模型看什么」和「拒绝时给什么候选」。

禁止（entity_operable 黑名单）= 对 AI 不可见：渲染层直接排除，模型视野里
不存在该设备；call_service 的 ⛔ 硬校验保留作兜底（拦会话中途被禁的残留 id）。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 诊断/属性类 domain：不作为可控项出现在视图（与 main.py 旧逻辑一致）
DIAGNOSTIC_DOMAINS = frozenset({"sensor", "binary_sensor"})


def derive_sub_name(full_name: str, device_name: str) -> str:
    """剥掉父设备名前缀，得到子实体短名。

    "A灯 会客厅灯 左键"（设备名"A灯"）→ "会客厅灯 左键"。
    用户自定义别名不含父名时（如直接叫"会客厅灯"）整个名就是短名——
    有区分度，比丢掉强。名字与设备名相同/为空 → 空串（调用方不显示）。
    """
    if not full_name or not device_name or full_name == device_name:
        return ""
    if full_name.startswith(device_name):
        return full_name[len(device_name):].lstrip(" -_·—").strip()
    return full_name


def entry_label(entry: dict) -> str:
    """条目的展示名：设备名 + 子功能短名（无短名时退完整名）。"""
    sub = entry.get("sub_name", "")
    if sub:
        return f"{entry.get('device_name', '')} {sub}".strip()
    return entry.get("name", "") or entry.get("entity_id", "")


async def _load_scope(scope: str) -> dict[str, str]:
    """读 DB prefs scope，失败返回空 dict（视图缺权限信息不致命）。"""
    try:
        from ..core.database import Database
        return await Database.get().prefs_get_by_scope(scope)
    except Exception:  # noqa: BLE001
        logger.warning("device_registry: 读取 %s 失败", scope, exc_info=True)
        return {}


async def build_device_snapshot(ha_service: Any, ha_client: Any) -> dict:
    """构建全量设备快照（AI 的唯一可选列表）。

    Returns:
        {
          "entries": [可控实体条目（已排除禁止项），含 sub_name/controls/note/
                      state（已翻转）/name（别名优先，match_devices 兼容）],
          "devices": [物理设备分组（含纯诊断设备，保标题行），每组带
                      visible_entries（该设备下可见的可控实体条目）],
          "service_defs": {domain: {svc: {"fields": [...]}}}（原始服务定义）
        }
    """
    from .entity_controls import resolve_controls
    from .semantic_map import flip_state_value

    grouped = await ha_service.get_all_devices_grouped()
    flat_devices = await ha_service.get_all_devices()
    flat_by_eid = {d["entity_id"]: d for d in flat_devices}
    raw_svc_defs = await ha_service.get_service_defs(
        ha_client, domains=set(d.get("domain", "") for d in flat_devices)
    )
    notes_map = await _load_scope("entity_note")
    operable_disabled = await _load_scope("entity_operable")

    entries_by_eid: dict[str, dict] = {}
    devices_view: list[dict] = []
    for dev in grouped.get("devices", []):
        dev_name = dev.get("name", "") or ""
        ents = dev.get("entities", [])
        controllable = [e for e in ents if e["domain"] not in DIAGNOSTIC_DOMAINS]
        # 多可控实体设备才需要子功能短名消歧；单可控实体设备保持原状
        # （把 MIoT 子名噪声的回归面压到最小）。
        multi = len(controllable) > 1

        dev_entries: list[dict] = []
        for e in controllable:
            eid = e["entity_id"]
            flat = flat_by_eid.get(eid)
            if flat is None:
                continue
            # 语义映射：对称翻转对设备预翻转 state（controls current 跟着对）
            try:
                state = await flip_state_value(eid, str(flat.get("state", "")))
            except Exception:  # noqa: BLE001
                state = str(flat.get("state", ""))
            flat = {**flat, "state": state}
            full_name = str(flat.get("name", "") or eid)
            entry = {
                "entity_id": eid,
                "domain": flat.get("domain", eid.split(".")[0]),
                "device_id": dev.get("device_id", ""),
                "device_name": dev_name,
                "sub_name": derive_sub_name(full_name, dev_name) if multi else "",
                "name": full_name,
                "area_id": flat.get("area_id"),
                "area_name": flat.get("area_name") or dev.get("area_name"),
                "state": state,
                "attributes": flat.get("attributes", {}),
                "controls": resolve_controls(flat, raw_svc_defs),
                "note": notes_map.get(eid, ""),
                # 视图层隐藏禁止项：entries 只收 operable 的，ai_operable 恒 True
                "ai_operable": eid not in operable_disabled,
            }
            if not entry["ai_operable"]:
                continue
            entries_by_eid[eid] = entry
            dev_entries.append(entry)

        devices_view.append({
            "device_id": dev.get("device_id", ""),
            "name": dev_name,
            "model": dev.get("model"),
            "manufacturer": dev.get("manufacturer"),
            "sw_version": dev.get("sw_version"),
            "area_id": dev.get("area_id"),
            "area_name": dev.get("area_name"),
            "summary": dev.get("summary", ""),
            "visible_entries": dev_entries,
        })

    return {
        "entries": list(entries_by_eid.values()),
        "devices": devices_view,
        "service_defs": raw_svc_defs,
    }


# ---------------------------------------------------------------------------
# 渲染器 — 三套视图共用同一快照
# ---------------------------------------------------------------------------

def render_catalog_text(snapshot: dict) -> str:
    """catalog 文本（system prompt 的 device_catalog 分支 + rule_service 解析源）。

    行格式 `- {entity_id} (类型:{domain}, 状态:{state}) 名称:{显示名}` 是
    rule_service._parse_ha_catalog 的正则契约，不能改；「名称:」由统一用父设备名
    改为「父设备名 + 子功能短名」——正则的名称分组是 (.+)，容忍追加文本。
    禁止设备不出现在任何行（对 AI 不可见）。
    """
    lines: list[str] = []
    for dev in snapshot["devices"]:
        header = f"# {dev.get('name', '')}"
        if dev.get("model"):
            header += f" ({dev['model']})"
        if dev.get("area_name"):
            header += f" [{dev['area_name']}]"
        lines.append(header)
        for ent in dev["visible_entries"]:
            display = entry_label(ent)
            lines.append(
                f"- {ent['entity_id']} (类型:{ent['domain']}, 状态:{ent['state']}) 名称:{display}"
            )
    return "\n".join(lines) if lines else "(暂无 HA 设备)"


def render_controls_text(snapshot: dict) -> str:
    """controls 文本（system prompt 的 device_controls 分支，后台预编译缓存）。"""
    from .entity_controls import controls_to_text
    blocks: list[str] = []
    for dev in snapshot["devices"]:
        ents = dev["visible_entries"]
        if not ents:
            continue
        lines = [f"{dev.get('name', '')}:"]
        for ent in ents:
            if not ent["controls"]:
                continue
            lines.append(controls_to_text(
                ent, ent["controls"], indent=1,
                note=ent["note"] or None, sub_name=ent["sub_name"] or None,
            ))
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else ""


def render_devices_brief(snapshot: dict) -> list[dict]:
    """get_entities 的 devices 视图：物理设备精简列表（含 entity_labels 供匹配指称）。"""
    brief: list[dict] = []
    for dev in snapshot["devices"]:
        ents = dev["visible_entries"]
        brief.append({
            "name": dev.get("name", ""),
            "model": dev.get("model"),
            "area": dev.get("area_name"),
            "summary": dev.get("summary", ""),
            "entity_ids": [e["entity_id"] for e in ents],
            "entity_labels": {e["entity_id"]: entry_label(e) for e in ents},
        })
    return brief


def render_entities_flat(snapshot: dict) -> list[dict]:
    """get_entities 的 entities 视图：扁平实体列表（含 _controls，供 call_service）。"""
    out: list[dict] = []
    for e in snapshot["entries"]:
        out.append({
            "entity_id": e["entity_id"],
            "domain": e["domain"],
            "name": e["name"],
            "state": e["state"],
            "attributes": e["attributes"],
            "area_id": e.get("area_id"),
            "area_name": e.get("area_name"),
            "_controls": e["controls"],
            "note": e["note"],
            "ai_operable": True,  # 禁止项已在快照层排除
        })
    return out
