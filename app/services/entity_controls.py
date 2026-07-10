"""
从 HA entity attributes + services 动态推导控件列表
服务端 Python 版前端 deviceCapabilities.js
全动态 — 零硬编码
"""
from __future__ import annotations
from typing import Any


def resolve_controls(entity: dict, services: dict) -> dict[str, dict]:
    controls: dict[str, dict] = {}
    attrs = entity.get("attributes") or {}
    domain = entity["entity_id"].split(".")[0]
    domain_svcs = services.get(domain, {})

    # 属性名集合（含拆词，用于概念匹配）+ state 值
    attr_names = set(attrs.keys())
    state_val = str(entity.get("state", ""))
    if state_val:
        attr_names.add(state_val)
    for name in list(attr_names):
        for word in name.split("_"):
            if len(word) >= 2:
                attr_names.add(word)

    # 1. 枚举：数组属性 → service field 匹配
    for attr_name, attr_value in attrs.items():
        if not isinstance(attr_value, list) or len(attr_value) < 2:
            continue
        if attr_name.startswith("supported_"):
            continue
        target = attr_name[:-1] if attr_name.endswith("s") else attr_name
        match = _find_field(domain_svcs, target, attr_names)
        if not match:
            continue
        current = attrs.get(target, entity.get("state"))
        controls[target] = _enum(match["service"], match["field"], attr_value, current)

    # 1b. 单数借助复数
    for attr_name, attr_value in list(attrs.items()):
        if isinstance(attr_value, list) or not isinstance(attr_value, str):
            continue
        plural = attr_name + "s"
        options = attrs.get(plural)
        if not isinstance(options, list) or len(options) < 2:
            continue
        match = _find_field(domain_svcs, attr_name, attr_names)
        if not match:
            continue
        controls[attr_name] = _enum(match["service"], match["field"], options, attr_value)

    # 2. 滑块：数值属性 → service field 匹配
    for attr_name, attr_value in attrs.items():
        # 动态：current_X 且 entity 中同时存在 X 属性 → current_X 是传感器读数，跳过
        if attr_name.startswith("current_"):
            stripped = attr_name[8:]
            if stripped in attrs:
                continue
            m = _find_field(domain_svcs, attr_name, None) or _find_field(domain_svcs, stripped, attr_names)
            if not m:
                continue
        if attr_value is None or not isinstance(attr_value, (int, float)):
            continue
        if attr_name.startswith("supported_") or attr_name.endswith("_step"):
            continue
        if attr_name.startswith("min_") or attr_name.startswith("max_"):
            continue

        param_name = attr_name
        match = _find_field_pct(domain_svcs, param_name)
        if not match and attr_name.startswith("current_"):
            match = _find_field(domain_svcs, attr_name[8:], attr_names)
        if not match:
            continue
        # brightness 原始值跳过，只用 _pct
        if match["field"] == "brightness" and not match["field"].endswith("_pct"):
            continue

        min_v, max_v, step = 0, 100, 1
        min_key = _attr_key(attrs, "min", attr_name)
        max_key = _attr_key(attrs, "max", attr_name)
        step_key = _attr_key(attrs, "", attr_name, "_step")
        if min_key:
            min_v = attrs[min_key]
        if max_key:
            max_v = attrs[max_key]
        if step_key:
            step = attrs[step_key]

        current = attr_value if attr_value is not None else min_v
        is_pct = "_pct" in match["field"]
        if is_pct:
            current = round(current * 100 / max(attr_value, 1))
            min_v, max_v = 0, 100

        unit = _unit(attr_name, attrs)
        controls[attr_name] = _slider(match["service"], match["field"], min_v, max_v, step, current, unit)

    # 3. 动作控件：无参服务（fields 除 entity_id 外为空）
    for svc_name, svc_def in domain_svcs.items():
        if svc_name.startswith("set_") or svc_name.startswith("select_"):
            continue
        fields = svc_def.get("fields", [])
        non_entity = [f for f in fields if f != "entity_id"]
        if non_entity:
            continue
        if svc_name in controls:
            continue
        # 动态概念匹配：service 名的词必须在 entity 属性名中出现过
        words = svc_name.split("_")
        related = _concept_match(words, attr_names, domain, domain_svcs)
        if not related:
            continue
        controls[svc_name] = _action(svc_name)

    # 4. 反推缺失的 _pct 滑块（如灯关时无 brightness 属性），只取最短字段名
    pct_candidates = []
    for svc_name, svc_def in domain_svcs.items():
        if svc_name not in ("turn_on",):
            continue
        for f in svc_def.get("fields", []):
            if f == "entity_id":
                continue
            if not f.endswith("_pct"):
                continue
            base = f[:-4]
            if base in controls or f in controls:
                continue
            if base in attr_names:
                pct_candidates.append((f.count("_"), f, svc_name))
    if pct_candidates:
        pct_candidates.sort()
        _, f, svc_name = pct_candidates[0]
        controls[f] = _slider(svc_name, f, 0, 100, 1, 0, "")

    return controls


def _concept_match(words: list[str], attr_names: set, domain: str, domain_svcs: dict) -> bool:
    """动态：过滤掉 entity 不支持的 specialization（如 tilt 属性不存在时过滤 open_cover_tilt）。
    规则：若 service 名中有词不匹配 entity 且去掉该词后仍是有效 service → 跳过"""
    for w in words:
        if w == domain:
            continue
        if w in attr_names:
            continue
        for a in attr_names:
            if w in a or a in w:
                break
        else:
            remaining = [x for x in words if x != w]
            remaining_name = "_".join(remaining)
            if remaining_name in domain_svcs:
                return False
    return True


def controls_to_text(entity: dict, controls: dict) -> str:
    name = (entity.get("attributes") or {}).get("friendly_name", "") or entity["entity_id"]
    eid = entity["entity_id"]
    lines = [f"{name} ({eid})"]
    if not controls:
        lines.append("  (no controls)")
        return "\n".join(lines)

    for attr, ctrl in controls.items():
        label = attr.replace("_", " ").title()
        domain = entity["entity_id"].split(".")[0]

        if ctrl["type"] == "slider":
            u = ctrl.get("unit", "")
            lines.append(f"  {label} — {ctrl['min']}{u}~{ctrl['max']}{u}, now {ctrl['current']}{u}")
            lines.append(f"    domain={domain} | service={ctrl['service']} | param={ctrl['param']}")
        elif ctrl["type"] == "enum":
            opts = "|".join(str(o) for o in ctrl["options"])
            lines.append(f"  {label} — options: {opts}, now {ctrl['current']}")
            lines.append(f"    domain={domain} | service={ctrl['service']} | param={ctrl['param']}")
        elif ctrl["type"] == "action":
            lines.append(f"  {label} — action")
            lines.append(f"    domain={domain} | service={ctrl['service']}")

    return "\n".join(lines)


# ===== 内部工具函数 =====

def _enum(svc, param, options, current):
    return {"type": "enum", "service": svc, "param": param, "options": options, "current": current}


def _slider(svc, param, min_v, max_v, step, current, unit):
    return {"type": "slider", "service": svc, "param": param, "min": min_v, "max": max_v, "step": step, "current": current, "unit": unit}


def _action(svc):
    return {"type": "action", "service": svc, "param": None}


def _find_field(domain_svcs: dict, field_name: str, attr_names: set | None = None) -> dict | None:
    candidates = []
    for svc_name, svc_def in domain_svcs.items():
        if field_name in svc_def.get("fields", []):
            candidates.append((len(svc_def.get("fields", [])), svc_name, field_name))
    if candidates:
        candidates.sort()
        _, svc_name, fname = candidates[0]
        return {"service": svc_name, "field": fname}
    pct_field = field_name + "_pct"
    for svc_name, svc_def in domain_svcs.items():
        if pct_field in svc_def.get("fields", []):
            return {"service": svc_name, "field": pct_field}
    if attr_names:
        for svc_name, svc_def in domain_svcs.items():
            for f in svc_def.get("fields", []):
                for part in f.split("_"):
                    if part in attr_names and len(part) >= 3:
                        return {"service": svc_name, "field": f}
    return None


def _find_field_pct(domain_svcs: dict, field_name: str) -> dict | None:
    pct_field = field_name + "_pct"
    for svc_name, svc_def in domain_svcs.items():
        if pct_field in svc_def.get("fields", []):
            return {"service": svc_name, "field": pct_field}
    return _find_field(domain_svcs, field_name)


def _attr_key(attrs: dict, prefix: str, target: str, suffix: str = "") -> str | None:
    exact = f"{prefix}_{target}{suffix}"
    if exact in attrs:
        return exact
    no_sep = f"{prefix}{target}{suffix}"
    if no_sep in attrs:
        return no_sep
    for key in attrs:
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue
        after = key[len(prefix):len(key) - len(suffix)]
        base = after[1:] if after.startswith("_") else after
        if base == target:
            continue
        if target.startswith(base) and len(base) >= 3:
            return key
    return None


def _unit(attr_name: str, attrs: dict) -> str:
    return attrs.get("unit_of_measurement", "") or ""
