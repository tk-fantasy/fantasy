from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Callable

from ..clients.llm_chat_client import LlmChatClient
from ..utils.json_extractor import extract_json_from_content
from ..utils.text_match import fuzzy_match
from .entity_controls import resolve_controls, controls_to_text

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


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

        与 scheduler_service._resolve_reminder_client 同一模式：
        resolve_key_for_role_user 拿到 per-user key → 构造独立 LlmChatClient，
        覆盖 _api_key/_base_url/_model/_enabled=True（关键：_enabled=True 绕过全局 llm.enabled 开关）。
        """
        if user_id:
            try:
                from ..core.key_resolver import resolve_key_for_role_user
                from ..clients.llm_chat_client import LlmChatClient as _LlmChatClient
                key_info = await resolve_key_for_role_user("chat", user_id)
                if key_info and key_info.get("api_key"):
                    client = _LlmChatClient(role="chat")
                    client._api_key = key_info["api_key"]
                    client._base_url = key_info["base_url"]
                    client._model = key_info["model"]
                    client._enabled = True
                    return client
            except Exception:
                logger.debug("Failed to resolve per-user rule client, using global", exc_info=True)
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

    async def build_rule(self, text: str, user_id: str = "") -> dict:
        # 先按 user_id 解析 per-user client（无配置回退全局 self._client），
        # 再检查 enabled —— per-user client 解析成功时 _enabled=True，可绕过全局占位符 key 的禁用态
        client = await self._resolve_client(user_id)
        if not client.enabled:
            return self._fallback_rule(text)

        # 获取 HA 设备目录（用于 prompt）
        ha_catalog = ""
        devices = []
        if self._ha_catalog_provider is not None:
            try:
                ha_catalog = self._ha_catalog_provider()
                devices = self._parse_ha_catalog(ha_catalog)
            except Exception:
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
        raw_svc_defs = {}
        if full_devices and services_info:
            raw_svc_defs = {
                domain: {svc: {"fields": fields} for svc, fields in svcs.items()}
                for domain, svcs in services_info.items()
            }
            # 为所有 full_devices 预计算 _controls（用于校验 + 提示词）
            for d in full_devices:
                d["_controls"] = resolve_controls(d, raw_svc_defs)
            # domain 过滤后生成中文 controls
            filtered_devices = _filter_devices(text, full_devices)
            c_lines = []
            for d in filtered_devices:
                controls = d.get("_controls", {})
                if controls:
                    c_lines.append(controls_to_text(d, controls))
            controls_text = "\n\n".join(c_lines) if c_lines else ""

        # 构建设备列表文本（entity_id 映射）- 同样只包含匹配的 devices
        device_list_text = ""
        if devices:
            filtered = _filter_devices(text, devices)
            lines = []
            for d in filtered:
                lines.append(f"- {d['name']} (entity_id: {d['entity_id']})")
            device_list_text = "\n".join(lines)
        else:
            device_list_text = "(暂无可用设备)"

        from .prompt_service import RULE_SYSTEM_PROMPT_TEMPLATE
        system_prompt = RULE_SYSTEM_PROMPT_TEMPLATE.format(
            controls_text=controls_text,
            device_list_text=device_list_text,
        )

        messages = [
            {"role": "system", "content": system_prompt},
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
                return self._fallback_rule(text)

            # 兜底:确保关键字段存在
            parsed.setdefault("name", text[:20])
            parsed.setdefault("condition", "")
            parsed.setdefault("actions", [])
            parsed.setdefault("action_descriptions", [])
            parsed.setdefault("cooldown_seconds", 10)
            parsed.setdefault("summary", text)

            # 校验 actions（使用完整设备数据，带 attributes）
            errors = self._validate_actions(parsed.get("actions", []), full_devices, services_info)
            
            if not errors:
                # 校验通过
                return parsed
            
            # 校验失败，还有重试机会
            if attempt < MAX_RETRIES:
                error_text = "\n".join(f"- {e}" for e in errors)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user", 
                    "content": f"你生成的规则有以下错误，请修正后重新生成完整的 JSON：\n{error_text}"
                })
                logger.info(f"Rule validation failed (attempt {attempt + 1}), retrying: {errors}")
            else:
                # 最后一次也失败了，记录日志但返回结果
                logger.warning(f"Rule validation failed after {MAX_RETRIES + 1} attempts: {errors}")
                return parsed

        return self._fallback_rule(text)

    def _fallback_rule(self, text: str) -> dict:
        return {
            "name": text[:20],
            "condition": "",
            "actions": [],
            "action_descriptions": [],
            "cooldown_seconds": 10,
            "summary": text,
        }

    def _parse_json(self, content: str) -> dict:
        extracted = extract_json_from_content(content)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            return {}
