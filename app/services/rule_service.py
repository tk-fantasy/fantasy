from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable

from ..clients.client_factory import build_per_user_chat_client
from ..clients.llm_chat_client import LlmChatClient
from ..core.config import get_config
from ..utils.json_extractor import extract_json_from_content
from ..utils.text_match import fuzzy_match, match_devices
from .entity_controls import resolve_controls, controls_to_text

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

# 自动匹配修复时排除的不可控 domain：传感器/诊断实体读得了但控不了，
# 匹配到它们生成的动作永远执行不出效果（与 device_registry.DIAGNOSTIC_DOMAINS 同口径）
_UNCONTROLLABLE_DOMAINS = frozenset({"sensor", "binary_sensor"})

# 开/关意图在不同 domain 下的标准 service（不在 services_info 时回退 turn_on/turn_off）
_INTENT_SERVICE_BY_DOMAIN = {
    "cover": {"on": "open_cover", "off": "close_cover"},
    "lock": {"on": "unlock", "off": "lock"},
}


def _service_intent(service: str) -> str:
    """把原 service 归约为开/关意图：open_cover/open_lock/turn_on → on，close/turn_off → off。"""
    s = (service or "").lower()
    if "off" in s or "close" in s:
        return "off"
    return "on"


def _pick_service(domain: str, intent: str, services_info: dict) -> str:
    """为匹配到的 domain 选一个该意图下真实存在的 service。

    优先 domain 特化（cover→open_cover 等），回退 turn_on/turn_off，
    都不在 services_info 里时再宽匹配语义词，最后放弃（调用方跳过修复）。
    """
    available = services_info.get(domain) or {}
    preferred = _INTENT_SERVICE_BY_DOMAIN.get(domain, {}).get(intent, f"turn_{intent}")
    if preferred in available:
        return preferred
    for svc in available:
        if intent == "on" and ("on" in svc or "open" in svc):
            return svc
        if intent == "off" and ("off" in svc or "close" in svc):
            return svc
    return ""


def _filter_devices(query: str, devices: list[dict]) -> list[dict]:
    """动态过滤：用户查询匹配 device friendly_name → 只返回相关 devices。无匹配则返回全部。"""
    if not query or not devices:
        return devices
    match = []
    for d in devices:
        name = str(d.get("name", "") or d.get("entity_id", ""))
        eid = str(d.get("entity_id", ""))
        if fuzzy_match(query, name) or fuzzy_match(query, eid):
            match.append(d)
    return match


def _is_known_camera(camera_id: str) -> bool:
    """camera_id 是否是真实存在的摄像头。

    取不到列表（容器未装配 / camera_manager 缺失 / 列表为空 / 抛错）一律放行——
    与 pending_rules.find_missing_entities 的「校验失败放行」口径一致，不因校验
    手段不可用而锁死修改。
    """
    try:
        # 函数内导入：container 反向依赖 services，模块级 import 会成环
        from ..container import get_container
        manager = getattr(get_container(), "camera_manager", None)
        cameras = manager.list_cameras() if manager is not None else None
    except Exception:  # noqa: BLE001
        return True
    ids = {str(c.get("id", "")) for c in (cameras or []) if isinstance(c, dict)}
    return not ids or camera_id in ids


class RuleService:
    def __init__(self, client: LlmChatClient | None = None) -> None:
        self._client = client or LlmChatClient()
        self._ha_catalog_provider: Callable[[], str] | None = None
        self._ha_services_provider: Callable[[], dict] | None = None
        self._ha_devices_provider: Callable[[], Awaitable[list[dict]]] | None = None

    def set_ha_catalog_provider(self, provider: Callable[[], str]) -> None:
        """注入 HA 设备目录，供规则解析提示词使用。"""
        self._ha_catalog_provider = provider

    def set_ha_services_provider(self, provider: Callable[[], dict]) -> None:
        """注入 HA 服务定义，供规则解析提示词使用。"""
        self._ha_services_provider = provider

    def set_ha_devices_provider(self, provider: Callable[[], Awaitable[list[dict]]]) -> None:
        """注入 HA 完整设备数据提供者，用于校验动作参数。"""
        self._ha_devices_provider = provider

    async def _resolve_client(self, user_id: str = "") -> LlmChatClient:
        """按 user_id 解析 per-user chat client；无配置则回退全局 self._client。

        per-user 客户端构造走 build_per_user_chat_client（强制 _enabled=True，绕过全局
        llm.enabled 开关）。无 per-user 配置时回退注入的全局 self._client。
        """
        per_user = await build_per_user_chat_client("chat", user_id, force_enabled=True)
        if per_user is not None:
            return per_user
        return self._client

    def _parse_ha_catalog(self, catalog: str) -> list[dict]:
        """从 catalog 字符串解析出设备列表 [{entity_id, name, domain}]"""
        devices = []
        pattern = r'- (\S+) \(类型:(\w+), 状态:[^)]+\) 名称:(.+)'
        for line in catalog.split('\n'):
            match = re.match(pattern, line.strip())
            if match:
                devices.append({
                    'entity_id': match.group(1),
                    'domain': match.group(2),
                    'name': match.group(3).strip(),
                })
        return devices

    def _validate_actions(self, actions: list[dict], devices: list[dict], services_info: dict) -> list[str]:
        """校验 actions，返回错误列表。空列表表示全部合法。"""
        errors = []
        valid_entity_ids = {d['entity_id'] for d in devices}
        
        for i, action in enumerate(actions):
            tool_input = action.get('mcp_tool_input', {})
            domain = tool_input.get('domain', '')
            service = tool_input.get('service', '')
            entity_id = tool_input.get('entity_id', '')
            data = tool_input.get('data') or {}

            # 归一化：确保 data 不是 None
            tool_input['data'] = data

            # 检查 mcp_tool_name
            tool_name = action.get('mcp_tool_name', '')
            if tool_name != 'ha_devices___call_service':
                errors.append(f"动作{i+1}: mcp_tool_name 必须是 'ha_devices___call_service'，实际是 '{tool_name}'")
            
            # 检查 domain
            if not domain:
                errors.append(f"动作{i+1}: 缺少 domain 字段")
            elif services_info and domain not in services_info:
                errors.append(f"动作{i+1}: domain '{domain}' 不存在，可用的 domain: {list(services_info.keys())}")
            
            # 检查 service
            if not service:
                errors.append(f"动作{i+1}: 缺少 service 字段")
            elif domain and services_info:
                domain_services = services_info.get(domain, {})
                if service not in domain_services:
                    errors.append(f"动作{i+1}: service '{service}' 在 domain '{domain}' 中不存在，可用的 service: {list(domain_services.keys())}")
            
            # 检查 entity_id
            if not entity_id:
                errors.append(f"动作{i+1}: 缺少 entity_id 字段")
            elif entity_id not in valid_entity_ids:
                errors.append(f"动作{i+1}: entity_id '{entity_id}' 不存在，可用的 entity_id: {list(valid_entity_ids)}")
            
            # 检查 data 参数
            if domain and service and services_info:
                domain_services = services_info.get(domain, {})
                service_fields = domain_services.get(service, [])
                
                # 检查 data 里的字段是否是该 service 需要的
                for field in data:
                    if field not in service_fields:
                        errors.append(f"动作{i+1}: data 中的字段 '{field}' 不是 service '{service}' 需要的参数，需要的参数: {service_fields}")
                
                # 检查枚举类型的值是否合法
                for field in service_fields:
                    if field in data:
                        device = next((d for d in devices if d['entity_id'] == entity_id), None)
                        if device:
                            attrs = device.get('attributes', {})
                            plural_attr = field + 's'
                            if plural_attr in attrs and isinstance(attrs[plural_attr], list):
                                valid_values = attrs[plural_attr]
                                if data[field] not in valid_values:
                                    errors.append(f"动作{i+1}: data.{field} 的值 '{data[field]}' 不在可选值 {valid_values} 中")
                
                # 检查 service 是否与该设备的可控 param 有关联（只在 service 有 fields 时检查）
                if service_fields:
                    device = next((d for d in devices if d['entity_id'] == entity_id), None)
                    if device:
                        controls = device.get("_controls", {})
                        ctrl_params = {c.get("param") for c in controls.values() if c.get("param")}
                        if ctrl_params and not any(f in ctrl_params for f in service_fields):
                            errors.append(
                                f"动作{i+1}: service '{service}' 与设备 '{entity_id}' 不匹配，"
                                f"可控参数: {ctrl_params}，该 service 字段: {service_fields}"
                            )

        return errors

    def _auto_repair_actions(self, parsed: dict, full_devices: list[dict], services_info: dict) -> list[dict]:
        """确定性自动匹配：LLM 反复修不对时，把幻觉 entity_id 强制替换为最接近的真实设备。

        只在重试耗尽后调用（用户明确要求"就算错了也先给一个能确认的草稿，弹窗里
        二次核对"）。匹配 query 依次取动作中文描述 → 规则 summary/name——幻觉 id
        本身是英文乱码，当 query 只会匹配到噪声。替换会同步修正 domain/service，
        替换明细记入 parsed["auto_corrections"] 供弹窗横幅与模型复述使用。

        完全匹配不到的动作保持原样不动（绝不硬塞不相干设备），由调用方按
        validation_errors 拦截——塞一个无关设备进去，用户没核出来确认了，比
        确认不了更危险。
        """
        valid = {d["entity_id"] for d in full_devices}
        descriptions = parsed.get("action_descriptions") or []
        corrections: list[dict] = []
        for i, action in enumerate(parsed.get("actions") or []):
            tool_input = action.get("mcp_tool_input") or {}
            entity_id = str(tool_input.get("entity_id", "") or "")
            if not entity_id or entity_id in valid:
                continue
            query = ""
            for source in (descriptions[i] if i < len(descriptions) else "",
                           parsed.get("summary", ""), parsed.get("name", "")):
                if source and str(source).strip():
                    query = str(source)
                    break
            candidates = [d for d in match_devices(query, full_devices)
                          if str(d.get("entity_id", "")).split(".")[0] not in _UNCONTROLLABLE_DOMAINS]
            if not candidates:
                logger.warning("auto_repair: 动作%d 设备 %r 无近似匹配，保留待拦截", i + 1, entity_id)
                continue
            matched = candidates[0]
            new_domain = str(matched["entity_id"]).split(".")[0]
            new_service = _pick_service(new_domain, _service_intent(str(tool_input.get("service", ""))),
                                        services_info)
            if not new_service:
                logger.warning("auto_repair: %r 无可用 service，跳过修复", matched["entity_id"])
                continue
            tool_input["domain"] = new_domain
            tool_input["service"] = new_service
            tool_input["entity_id"] = matched["entity_id"]
            # 原 data 是按幻觉设备的 service 生成的，字段大概率不适用新设备，重置为无参动作
            tool_input["data"] = {}
            corrections.append({
                "action_index": i,
                "from": entity_id,
                "to": matched["entity_id"],
                "to_name": matched.get("name", matched["entity_id"]),
                "query": query,
            })
            logger.info("auto_repair: 动作%d %r → %r (query=%r)", i + 1, entity_id,
                        matched["entity_id"], query)
        if corrections:
            parsed["auto_corrections"] = corrections
        return corrections

    def _friendly_device_hints(self, parsed: dict, full_devices: list[dict]) -> str:
        """为重试反馈附上人话设备对照，帮模型自愈。

        此前重试错误只贴全量拼音 entity_id（60+ 个乱码），模型对不上"门"是哪个，
        3 轮都修不对。这里按动作描述给出 top3 相关候选（友好名 + entity_id）；
        一个都匹配不上时给主控设备清单，让模型至少知道家里有什么。
        """
        hints: list[str] = []
        valid = {d["entity_id"] for d in full_devices}
        descriptions = parsed.get("action_descriptions") or []
        for i, action in enumerate(parsed.get("actions") or []):
            tool_input = action.get("mcp_tool_input") or {}
            entity_id = str(tool_input.get("entity_id", "") or "")
            if not entity_id or entity_id in valid:
                continue
            query = (descriptions[i] if i < len(descriptions) else "") or parsed.get("summary", "")
            candidates = [d for d in match_devices(query, full_devices)
                          if str(d.get("entity_id", "")).split(".")[0] not in _UNCONTROLLABLE_DOMAINS][:3]
            if candidates:
                listed = "、".join(f"{d.get('name')} ({d['entity_id']})" for d in candidates)
                hints.append(f"动作{i+1} 与「{query}」相关的真实设备: {listed}")
        if not hints:
            primary = [d for d in full_devices
                       if str(d.get("entity_id", "")).split(".")[0] not in _UNCONTROLLABLE_DOMAINS][:10]
            if primary:
                listed = "、".join(f"{d.get('name')} ({d['entity_id']})" for d in primary)
                hints.append(f"家里没有与描述相关的设备，现有可控设备: {listed}")
        return "\n".join(hints)

    async def _prepare_rule_context(self, filter_text: str, user_id: str = "") -> dict:
        """加载 HA 数据并构造规则解析 system prompt。

        build_rule（新建）和 revise_rule（修改）共用此方法，避免重复。
        filter_text 用于把 prompt 范围缩小到相关设备（无匹配则返回全部）。

        Returns:
            {"client", "system_prompt", "full_devices", "services_info"}；LLM 未启用时返回空 dict。
        """
        client = await self._resolve_client(user_id)
        if not client.enabled:
            return {}

        # 获取 HA 设备目录（用于 prompt）
        devices = []
        if self._ha_catalog_provider is not None:
            try:
                ha_catalog = self._ha_catalog_provider()
                devices = self._parse_ha_catalog(ha_catalog)
            except Exception:  # noqa: BLE001
                pass

        # 获取完整设备数据（带 attributes，用于校验）
        full_devices = []
        if self._ha_devices_provider is not None:
            try:
                full_devices = await self._ha_devices_provider()
            except Exception:
                logger.warning("Failed to load full devices for validation", exc_info=True)

        # 获取 HA 服务定义，只保留有实际设备的 domain
        services_info = {}
        if self._ha_services_provider is not None:
            try:
                all_services = await self._ha_services_provider()
                device_domains = {d['domain'] for d in devices}
                services_info = {
                    domain: svcs for domain, svcs in all_services.items()
                    if domain in device_domains
                }
            except Exception:
                logger.warning("Failed to load HA services for rule generation", exc_info=True)

        # 构建设备可控项中文文本（替代 JSON 服务列表 + device attributes）
        controls_text = ""
        if full_devices and services_info:
            raw_svc_defs = {
                domain: {svc: {"fields": fields} for svc, fields in svcs.items()}
                for domain, svcs in services_info.items()
            }
            # 用户自定义备注：让规则生成 LLM 也看到设备怪癖（如继电器反转语义）
            notes_map: dict[str, str] = {}
            try:
                from ..core.database import Database
                notes_map = await Database.get().prefs_get_by_scope("entity_note")
            except Exception:
                logger.warning("Failed to load entity notes for rule generation", exc_info=True)
            # 为所有 full_devices 预计算 _controls（用于校验 + 提示词）
            for d in full_devices:
                d["_controls"] = resolve_controls(d, raw_svc_defs)
            # domain 过滤后生成中文 controls
            filtered_devices = _filter_devices(filter_text, full_devices)
            c_lines = []
            for d in filtered_devices:
                controls = d.get("_controls", {})
                if controls:
                    c_lines.append(
                        controls_to_text(d, controls, note=notes_map.get(d.get("entity_id", "")))
                    )
            controls_text = "\n\n".join(c_lines) if c_lines else ""

        # 构建设备列表文本（entity_id 映射）- 同样只包含匹配的 devices
        if devices:
            filtered = _filter_devices(filter_text, devices)
            lines = [f"- {d['name']} (entity_id: {d['entity_id']})" for d in filtered]
            device_list_text = "\n".join(lines)
        else:
            device_list_text = "(暂无可用设备)"

        from .prompt_service import RULE_SYSTEM_PROMPT_TEMPLATE
        system_prompt = RULE_SYSTEM_PROMPT_TEMPLATE.format(
            controls_text=controls_text,
            device_list_text=device_list_text,
        )
        return {
            "client": client,
            "system_prompt": system_prompt,
            "full_devices": full_devices,
            "services_info": services_info,
        }

    async def build_rule(self, text: str, user_id: str = "", camera_id: str = "") -> dict:
        # 复用 _prepare_rule_context 加载 HA 数据 + 构造 system prompt
        ctx = await self._prepare_rule_context(text, user_id)
        if not ctx:
            return self._fallback_rule(text, camera_id=camera_id)
        client = ctx["client"]
        full_devices = ctx["full_devices"]
        services_info = ctx["services_info"]

        messages = [
            {"role": "system", "content": ctx["system_prompt"]},
            {"role": "user", "content": f"请把这句话解析成自动化规则 JSON: {text}"},
        ]

        # 重试循环
        for attempt in range(MAX_RETRIES + 1):
            content = await client.chat(messages, 20)
            parsed = self._parse_json(content)
            if not parsed:
                if attempt < MAX_RETRIES:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "JSON 解析失败，请重新生成有效的 JSON。"})
                    continue
                # 兜底规则同样保留摄像头绑定（与下方 setdefault 透传语义一致，
                # 否则绑定摄像头的规则静默退化成全局规则）
                return self._fallback_rule(text, camera_id)

            # 兜底:确保关键字段存在
            parsed.setdefault("name", text[:20])
            parsed.setdefault("condition", "")
            # type 归一化到合法值；LLM 漏输出或乱填时兜底 vision
            _t = str(parsed.get("type", "vision") or "vision").strip().lower()
            parsed["type"] = _t if _t in ("time", "weather", "vision") else "vision"
            parsed.setdefault("actions", [])
            parsed.setdefault("action_descriptions", [])
            parsed.setdefault("cooldown_seconds", get_config("automation.default_cooldown_seconds", 5))
            parsed.setdefault("summary", text)
            parsed.setdefault("camera_id", camera_id)   # Task 5:透传摄像头绑定

            # 校验 actions（使用完整设备数据，带 attributes）
            errors = self._validate_actions(parsed.get("actions", []), full_devices, services_info)

            if not errors:
                return parsed

            # 校验失败，还有重试机会
            if attempt < MAX_RETRIES:
                error_text = "\n".join(f"- {e}" for e in errors)
                device_hints = self._friendly_device_hints(parsed, full_devices)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"你生成的规则有以下错误，请修正后重新生成完整的 JSON：\n{error_text}"
                               f"\n\n设备对照提示（entity_id 必须从这些里取）：\n{device_hints}"
                })
                logger.info("Rule validation failed (attempt %d), retrying: %s", attempt + 1, errors)
            else:
                logger.warning("Rule validation failed after %d attempts: %s", MAX_RETRIES + 1, errors)
                # 确定性兜底：LLM 反复修不对时，代码层强制匹配最接近的真实设备并替换，
                # 替换明细挂在 auto_corrections 供弹窗核对；仍修不好的记 validation_errors
                # 交给调用方拦截（不出带非法设备的死局草稿）。
                self._auto_repair_actions(parsed, full_devices, services_info)
                remaining = self._validate_actions(parsed.get("actions", []), full_devices, services_info)
                if remaining:
                    parsed["validation_errors"] = remaining
                    logger.warning("Rule auto-repair incomplete: %s", remaining)
                return parsed

        return self._fallback_rule(text, camera_id)

    @staticmethod
    def _resolve_revised_camera(parsed: dict, current_cam: str) -> str:
        """决定修改后规则的 camera_id。

        三条规则：
        1. LLM 没输出 → 保留原值（未提到的字段保持原样）
        2. LLM 输出了但不是真实摄像头 → 重置回原值。幻觉 id 会让规则绑到不存在
           的那一路，automation_service 按 camera_id 过滤 → 永不触发，且界面上看
           不出问题，比不改更糟
        3. type 改成 time/weather → 清空。camera_id 对非视觉规则没有意义，留着会
           被 ruleMismatch 标 orange（定时/天气规则绑摄像头）
        """
        new_cam = str(parsed.get("camera_id", "") or "").strip()
        if not new_cam:
            resolved = current_cam
        elif new_cam != current_cam and not _is_known_camera(new_cam):
            logger.info("revise_rule: LLM 给出未知 camera_id %r，重置回 %r", new_cam, current_cam)
            resolved = current_cam
        else:
            resolved = new_cam
        if str(parsed.get("type", "") or "").strip().lower() in ("time", "weather"):
            return ""
        return resolved

    async def revise_rule(self, current_rule: dict, instruction: str, user_id: str = "") -> dict:
        """基于自然语言指令迭代修改已有规则（不落库，只返回预览）。

        复用 build_rule 的 HA 数据加载 + system prompt + 校验重试逻辑，只改 user prompt：
        把当前规则 JSON + 修改指令交给 LLM，让它输出完整新 JSON。

        Returns:
            {"rule": {...新规则 JSON...}, "summary": "一句中文说明改了什么"}
            LLM 未启用时返回 {"rule": current_rule, "summary": "...", "fallback": True}。
        """
        # filter_text 用修改指令 + 当前条件/动作描述，确保相关设备被拉进 prompt
        current_desc = " ".join([
            str(current_rule.get("condition", "")),
            " ".join(str(x) for x in current_rule.get("action_descriptions", []) or []),
        ])
        filter_text = f"{instruction} {current_desc}".strip()
        ctx = await self._prepare_rule_context(filter_text, user_id)
        if not ctx:
            return {"rule": current_rule, "summary": "LLM 未配置，无法修改", "fallback": True}

        client = ctx["client"]
        full_devices = ctx["full_devices"]
        services_info = ctx["services_info"]
        current_type = str(current_rule.get("type", "") or "")
        current_cam = str(current_rule.get("camera_id", "") or "").strip()
        # 只传 schema 子集，避免 id/enabled/时间戳噪声干扰 LLM。
        # camera_id 必须在内：它是视觉规则的路由字段，用户会说「改绑到门口摄像头」，
        # 不把它给 LLM 看就永远改不动（此前漏了，导致自然语言改绑静默 no-op）。
        current_brief = {
            k: current_rule.get(k)
            for k in ("name", "condition", "type", "actions", "action_descriptions",
                      "cooldown_seconds", "summary", "camera_id")
            if k in current_rule
        }

        messages = [
            {"role": "system", "content": ctx["system_prompt"]},
            {"role": "user", "content": (
                f"以下是当前规则的 JSON:\n{json.dumps(current_brief, ensure_ascii=False, indent=2)}\n\n"
                f"请按下面的指令修改这条规则，输出修改后的完整新 JSON（字段同 schema，"
                f"不要包含 id/enabled/created_at/updated_at/user_id 等元数据）：\n{instruction}\n\n"
                f"未提到的字段保持原样。额外在 JSON 中加一个 \"change_summary\" 字段，"
                f"用一句中文说明你这次改了什么。"
            )},
        ]

        summary = "已更新"
        for attempt in range(MAX_RETRIES + 1):
            content = await client.chat(messages, 20)
            parsed = self._parse_json(content)
            if not parsed:
                if attempt < MAX_RETRIES:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": "JSON 解析失败，请重新生成有效的 JSON。"})
                    continue
                return {"rule": current_rule, "summary": "修改失败：LLM 未返回有效 JSON", "fallback": True}

            # 抽取 change_summary（弹出，不进规则本体）
            summary = str(parsed.pop("change_summary", "") or "已更新") or "已更新"

            # 兜底关键字段；type 缺失时保留原规则的 type（不兜底 vision，避免改错路由）
            parsed.setdefault("name", current_rule.get("name", ""))
            parsed.setdefault("condition", current_rule.get("condition", ""))
            parsed.setdefault("type", current_type or "vision")
            parsed.setdefault("actions", current_rule.get("actions", []))
            parsed.setdefault("action_descriptions", current_rule.get("action_descriptions", []))
            parsed.setdefault("cooldown_seconds", current_rule.get("cooldown_seconds",
                              get_config("automation.default_cooldown_seconds", 5)))
            parsed.setdefault("summary", current_rule.get("summary", ""))
            parsed["camera_id"] = self._resolve_revised_camera(parsed, current_cam)

            # 校验 actions
            errors = self._validate_actions(parsed.get("actions", []), full_devices, services_info)
            if not errors:
                return {"rule": parsed, "summary": summary}

            if attempt < MAX_RETRIES:
                error_text = "\n".join(f"- {e}" for e in errors)
                device_hints = self._friendly_device_hints(parsed, full_devices)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"你修改后的规则有以下错误，请修正后重新生成完整的 JSON：\n{error_text}"
                               f"\n\n设备对照提示（entity_id 必须从这些里取）：\n{device_hints}",
                })
                logger.info("Rule revise validation failed (attempt %d): %s", attempt + 1, errors)
            else:
                logger.warning("Rule revise validation failed after %d attempts: %s", MAX_RETRIES + 1, errors)
                # 与 build_rule 同款确定性兜底：修不好才挂 validation_errors
                self._auto_repair_actions(parsed, full_devices, services_info)
                remaining = self._validate_actions(parsed.get("actions", []), full_devices, services_info)
                if remaining:
                    parsed["validation_errors"] = remaining
                    logger.warning("Rule revise auto-repair incomplete: %s", remaining)
                return {"rule": parsed, "summary": summary}

        return {"rule": current_rule, "summary": "修改失败", "fallback": True}

    async def explain_rule(self, current_rule: dict, question: str, user_id: str = "") -> str:
        """plan 模式：用自然语言回答关于当前规则的提问（只读，不修改）。

        不需要 HA 设备数据（规则 JSON 本身已含 condition/actions/entity_id），
        只走一次 LLM 调用。失败时返回兜底文本。
        """
        client = await self._resolve_client(user_id)
        if not client.enabled:
            return "LLM 未配置，无法解释规则。"
        from .prompt_service import RULE_EXPLAIN_PROMPT
        # 只传 schema 子集，避免 id/时间戳噪声；camera_id 必须在内——
        # 用户会问"这条规则看的哪个摄像头"，不给他看就答不上
        brief = {
            k: current_rule.get(k)
            for k in ("name", "condition", "type", "actions", "action_descriptions",
                      "cooldown_seconds", "summary", "camera_id")
            if k in current_rule
        }
        # 实体对照：本部署的 entity_id 是设备厂商乱码（switch.ckcper_cn_...），
        # 靠"拼音翻译"认不出设备。附上 {entity_id → 友好名}，模型才答得出
        # "控制的 id/设备是哪个"而不编造。
        entity_lines: list[str] = []
        try:
            devices = await self._ha_devices_provider() if self._ha_devices_provider else []
        except Exception:  # noqa: BLE001 — 对照表拉不到时降级为无对照，不阻塞解释
            devices = []
        name_map = {d["entity_id"]: d.get("name", "") for d in devices or []}
        for action in current_rule.get("actions") or []:
            eid = str((action.get("mcp_tool_input") or {}).get("entity_id", "") or "")
            if eid:
                entity_lines.append(f"- {eid} → {name_map.get(eid) or '（对照表中无此设备）'}")
        entity_mapping = ("\n\n实体对照（entity_id → 设备名）：\n" + "\n".join(entity_lines)) if entity_lines else ""
        messages = [
            {"role": "system", "content": RULE_EXPLAIN_PROMPT},
            {"role": "user", "content": (
                f"规则 JSON:\n{json.dumps(brief, ensure_ascii=False, indent=2)}"
                f"{entity_mapping}\n\n"
                f"用户的问题：{question}"
            )},
        ]
        try:
            content = await client.chat(messages, 20)
            return str(content or "").strip() or "无法生成解释。"
        except Exception as e:
            logger.warning("explain_rule failed: %s", e, exc_info=True)
            return f"解释失败：{e}"

    def _fallback_rule(self, text: str, camera_id: str = "") -> dict:
        return {
            "name": text[:20],
            "condition": "",
            "type": "vision",
            "actions": [],
            "action_descriptions": [],
            "cooldown_seconds": get_config("automation.default_cooldown_seconds", 5),
            "summary": text,
            "camera_id": camera_id,   # Task 5:透传摄像头绑定
        }

    def _parse_json(self, content: str) -> dict:
        extracted = extract_json_from_content(content)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            return {}
