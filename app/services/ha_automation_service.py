"""HA 原生自动化生成服务 — 聊天一句话写进 Home Assistant（第三条路，阶段5）。

与 Aether 规则系统的分工：纯设备自动化（trigger→action，HA 界面可管理/可编辑）
落 HA；视觉/AI 判定/冷却语义落 Aether（automation_rule_*）。本服务把自然语言
解析成 HA automation schema：
- 动作格式与 Aether 规则同构（domain/service/entity_id/data），复用 rule_service
  的 _validate_actions / _auto_repair_actions 防幻觉链（实体/服务对照 HA 真实目录）；
- trigger/condition 用 HA 经典语法（platform 键），只做结构校验（每项必含
  platform / condition 键）——语义正确性由生成 prompt 约束。

两段式确认由工具层挂 pending_rules 机制（KIND_HA_AUTOMATION），本服务只管
「解析草稿」与「确认写入」两步。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 落 HA 时动作从 mcp 风格转 HA 现代语法：{"action": "domain.service",
# "target": {"entity_id": ...}, "data": {...}}
_MCP_TOOL_NAME = "ha_devices___call_service"


def _to_mcp_actions(actions: list[dict]) -> list[dict]:
    """把 LLM 输出的 {domain,service,entity_id,data} 包装成校验链的 mcp 风格。"""
    out = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        out.append({
            "mcp_tool_name": _MCP_TOOL_NAME,
            "mcp_tool_input": {
                "domain": str(a.get("domain", "") or ""),
                "service": str(a.get("service", "") or ""),
                "entity_id": str(a.get("entity_id", "") or ""),
                "data": a.get("data") if isinstance(a.get("data"), dict) else {},
            },
        })
    return out


def _to_ha_actions(mcp_actions: list[dict]) -> list[dict]:
    """mcp 风格动作 → HA 自动化 action 数组（现代 service-call 语法）。"""
    out = []
    for a in mcp_actions or []:
        t = a.get("mcp_tool_input") or {}
        domain = str(t.get("domain", "") or "")
        service = str(t.get("service", "") or "")
        entity_id = str(t.get("entity_id", "") or "")
        if not (domain and service):
            continue
        action: dict[str, Any] = {"action": f"{domain}.{service}"}
        if entity_id:
            action["target"] = {"entity_id": entity_id}
        if t.get("data"):
            action["data"] = t["data"]
        out.append(action)
    return out


def _validate_triggers(triggers: Any) -> list[str]:
    """trigger 结构校验：非空列表、每项 dict 且含 platform 键。"""
    if not isinstance(triggers, list) or not triggers:
        return ["trigger 缺失或为空（自动化至少要有一个触发器）"]
    errors = []
    for i, t in enumerate(triggers):
        if not isinstance(t, dict) or not str(t.get("platform", "") or "").strip():
            errors.append(f"trigger[{i}] 必须是含 platform 键的对象")
    return errors


def _validate_conditions(conditions: Any) -> list[str]:
    """condition 结构校验：列表（可空）、每项 dict 且含 condition 键。"""
    if conditions in (None, ""):
        return []
    if not isinstance(conditions, list):
        return ["condition 必须是数组（无条件传空数组）"]
    errors = []
    for i, c in enumerate(conditions):
        if not isinstance(c, dict) or not str(c.get("condition", "") or "").strip():
            errors.append(f"condition[{i}] 必须是含 condition 键的对象")
    return errors


# 阶段8 委托触发的编译产物白名单：LLM 产出之外的 platform/condition 一律拒绝
# （time/weather 场景用不到 calendar/zone 触发器等；白名单比校验修复链省事且
# 出错时直接走本地规则兜底，不阻塞创建）。
_DELEGATE_TRIGGER_PLATFORMS = frozenset(
    {"time", "time_pattern", "state", "numeric_state", "sun", "template"})
_DELEGATE_CONDITION_TYPES = frozenset(
    {"state", "numeric_state", "time", "sun", "zone", "template", "and", "or", "not"})


class HaAutomationService:
    def __init__(self, rule_service: Any, ha_client_ref: list | None = None) -> None:
        self._rule_service = rule_service
        self._ha_client_ref = ha_client_ref or [None]

    def set_refs(self, ha_client: Any) -> None:
        self._ha_client_ref[0] = ha_client

    @property
    def _ha_client(self) -> Any:
        return self._ha_client_ref[0]

    # ------------------------------------------------------------------
    # 第一步：解析草稿（不落 HA）
    # ------------------------------------------------------------------

    async def build_from_text(self, text: str, user_id: str = "") -> dict:
        """自然语言 → HA 自动化草稿（alias/trigger/condition/actions + 校验结果）。

        返回 dict：成功含 draft 字段（actions 保持 mcp 风格，写入时才转 HA 语法，
        便于复述与校验）；解析失败/校验不过含 error 字段。
        """
        rs = self._rule_service
        if rs is None:
            return {"error": "规则服务未就绪（目录装配依赖它）"}
        text = (text or "").strip()
        if not text:
            return {"error": "描述不能为空"}

        from .prompt_service import HA_AUTOMATION_PROMPT_TEMPLATE
        ctx = await rs._prepare_rule_context(text, user_id, system_template=HA_AUTOMATION_PROMPT_TEMPLATE)
        if not ctx:
            return {"error": "LLM 未启用或设备目录不可用"}
        client = ctx["client"]

        parsed = None
        messages = [
            {"role": "system", "content": ctx["system_prompt"]},
            {"role": "user", "content": f"请把这句话解析成 HA 自动化 JSON: {text}"},
        ]
        last_err = ""
        for _ in range(2):
            try:
                content = await client.chat(messages, 20)
                parsed = rs._parse_json(content)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                parsed = None
            if parsed:
                break
            messages.append({"role": "user", "content": "JSON 解析失败，请重新输出有效 JSON。"})
        if not parsed:
            return {"error": f"解析失败: {last_err or 'LLM 未返回有效 JSON'}"}
        if parsed.get("error"):
            return {"error": f"缺少触发条件: {parsed.get('error')}"}

        alias = str(parsed.get("alias", "") or text[:20]).strip()
        triggers = parsed.get("trigger") or parsed.get("triggers")
        conditions = parsed.get("condition", [])
        if conditions in (None, ""):
            conditions = parsed.get("conditions") or []
        mcp_actions = _to_mcp_actions(parsed.get("actions") or [])
        if not mcp_actions:
            return {"error": "解析出的自动化没有可执行动作"}

        errors = _validate_triggers(triggers) + _validate_conditions(conditions)
        if errors:
            return {"error": "；".join(errors)}

        # 动作防幻觉：复用规则系统的校验 + 近似实体自动修复链
        auto_corrections = []
        try:
            errors = rs._validate_actions(mcp_actions, ctx["full_devices"], ctx["services_info"])
            if errors:
                repair_input = {"actions": mcp_actions}
                rs._auto_repair_actions(repair_input, ctx["full_devices"], ctx["services_info"])
                mcp_actions = repair_input["actions"]
                auto_corrections = repair_input.get("auto_corrections") or []
                errors = rs._validate_actions(mcp_actions, ctx["full_devices"], ctx["services_info"])
        except Exception:  # noqa: BLE001
            logger.warning("HA automation action validation failed", exc_info=True)
            errors = []
        if errors:
            return {"error": f"动作校验未通过: {errors[0]}", "validation_errors": errors}

        return {
            "draft": {
                "alias": alias,
                "description": str(parsed.get("description", "") or ""),
                "trigger": triggers,
                "condition": conditions if isinstance(conditions, list) else [],
                "actions": mcp_actions,
                "action_descriptions": parsed.get("action_descriptions") or [],
                "auto_corrections": auto_corrections,
                "summary": text,
            }
        }

    # ------------------------------------------------------------------
    # 阶段8：时间/天气规则委托触发 —— 编译触发器 + 写回调自动化
    # ------------------------------------------------------------------

    async def component_ready(self) -> bool:
        """回调链路整体是否就绪：HA 侧组件已装配 + Aether 侧 APP_TOKEN 已设置。

        组件未装/未配置时 HA 也接受带未知服务的 automation 配置（保存成功、
        触发时才报"服务不存在"）——那是条静默死规则；APP_TOKEN 未设置时回调
        会被鉴权中间件 401，同样是静默死规则。两者都是委托前必须探掉的坑。
        组件在 async_setup_entry 成功后才注册 fire_rule 服务，探到服务即等价于
        host/token 已配置（config flow 两项都必填）。
        """
        from ..main import APP_TOKEN
        if not APP_TOKEN:
            return False
        client = self._ha_client
        if client is None or not hasattr(client, "get_services"):
            return False
        try:
            services = await client.get_services()
        except Exception:  # noqa: BLE001
            logger.debug("get_services failed for delegation probe", exc_info=True)
            return False
        for block in services or []:
            if isinstance(block, dict) and block.get("domain") == "aether_conversation":
                return "fire_rule" in (block.get("services") or {})
        return False

    async def compile_trigger(self, condition: str, rule_type: str, user_id: str = "") -> dict:
        """把 time/weather 规则的自然语言条件编译成 HA trigger/condition。

        与 build_from_text 的区别：只产出触发时机（动作固定为回调，由
        write_delegated 构造），且产物过 platform 白名单。任何失败返回
        {"error": ...}，调用方（工具层）据此回退本地规则路径。
        """
        rs = self._rule_service
        if rs is None:
            return {"error": "规则服务未就绪"}
        condition = (condition or "").strip()
        if not condition:
            return {"error": "条件描述为空"}

        from .prompt_service import HA_TRIGGER_COMPILE_PROMPT_TEMPLATE
        ctx = await rs._prepare_rule_context(
            condition, user_id, system_template=HA_TRIGGER_COMPILE_PROMPT_TEMPLATE)
        if not ctx:
            return {"error": "LLM 未启用或设备目录不可用"}

        # 天气实体单独补给 LLM：设备目录以可控设备为主，weather.* 不保证在列，
        # 而天气触发的 entity_id 只能从这里取
        weather_line = ""
        client_raw = self._ha_client
        if client_raw is not None and hasattr(client_raw, "get_states"):
            try:
                states = await client_raw.get_states()
                weather_ids = [str(s.get("entity_id", "")) for s in states or []
                               if str(s.get("entity_id", "")).startswith("weather.")]
                if weather_ids:
                    weather_line = f"\n可用天气实体: {', '.join(weather_ids[:5])}"
            except Exception:  # noqa: BLE001
                logger.debug("weather entity lookup failed", exc_info=True)

        type_hint = {"time": "时间触发", "weather": "天气触发"}.get(rule_type, rule_type or "时间/天气")
        parsed = None
        messages = [
            {"role": "system", "content": ctx["system_prompt"]},
            {"role": "user", "content": f"规则类型: {type_hint}{weather_line}\n"
                                        f"请把这条触发条件编译成 HA 触发配置 JSON: {condition}"},
        ]
        last_err = ""
        for _ in range(2):
            try:
                content = await ctx["client"].chat(messages, 20)
                parsed = rs._parse_json(content)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                parsed = None
            if parsed:
                break
            messages.append({"role": "user", "content": "JSON 解析失败，请重新输出有效 JSON。"})
        if not parsed:
            return {"error": f"解析失败: {last_err or 'LLM 未返回有效 JSON'}"}
        if parsed.get("error"):
            return {"error": f"HA 语法表达不了该条件: {parsed.get('error')}"}

        triggers = parsed.get("trigger") or parsed.get("triggers")
        conditions = parsed.get("condition", [])
        if conditions in (None, ""):
            conditions = parsed.get("conditions") or []
        errors = _validate_triggers(triggers) + _validate_conditions(conditions)
        if not errors:
            for t in triggers or []:
                if str(t.get("platform", "")) not in _DELEGATE_TRIGGER_PLATFORMS:
                    errors.append(f"触发器类型不在委托白名单: {t.get('platform')}")
            for c in conditions or []:
                if str(c.get("condition", "")) not in _DELEGATE_CONDITION_TYPES:
                    errors.append(f"条件类型不在委托白名单: {c.get('condition')}")
        if errors:
            return {"error": "；".join(errors)}
        return {"trigger": triggers, "condition": conditions if isinstance(conditions, list) else []}

    async def write_delegated(
        self, rule_id: str, name: str, trigger: list, condition: list,
        description: str = "", automation_id: str = "",
    ) -> dict:
        """委托触发自动化写入 HA：触发归 HA，动作回调 Aether 执行。

        automation_id 非空时覆盖写（revise 后重同步）；动作是 aether_conversation
        组件的 fire_rule 服务（组件配置里已有 host/token），不是设备服务调用。
        """
        client = self._ha_client
        if client is None or not hasattr(client, "create_automation"):
            return {"error": "HA 客户端不可用"}
        if not trigger:
            return {"error": "触发器为空"}
        config = {
            "alias": f"Aether·{name}",
            "description": description or f"Aether 规则委托触发（rule_id={rule_id}）",
            "trigger": trigger,
            "condition": condition or [],
            "action": [{"action": "aether_conversation.fire_rule",
                        "data": {"rule_id": str(rule_id)}}],
        }
        try:
            result = await client.create_automation(config, automation_id=str(automation_id or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("delegated automation write failed: %s", exc)
            return {"error": f"写入 Home Assistant 失败: {exc}"}
        return {"id": str(result.get("id", "")), "alias": config["alias"]}

    # ------------------------------------------------------------------
    # 第二步：确认写入 HA
    # ------------------------------------------------------------------

    async def create(self, draft: dict) -> dict:
        """把草稿写入 HA（POST automation config）。返回 {id, alias}。"""
        client = self._ha_client
        if client is None or not hasattr(client, "create_automation"):
            return {"error": "HA 客户端不可用"}
        config = {
            "alias": draft.get("alias", ""),
            "description": draft.get("description", ""),
            "trigger": draft.get("trigger") or [],
            "condition": draft.get("condition") or [],
            "action": _to_ha_actions(draft.get("actions") or []),
        }
        if not config["action"]:
            return {"error": "草稿没有可执行动作"}
        try:
            result = await client.create_automation(config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("HA automation create failed")
            return {"error": f"写入 Home Assistant 失败: {exc}"}
        automation_id = str(result.get("id", ""))
        logger.info("HA automation created: '%s' (%s)", config["alias"], automation_id)
        return {"id": automation_id, "alias": config["alias"]}

    async def list_automations(self) -> list[dict]:
        """HA 自动化列表（轻量化；数据来自 automation 实体，见 ha_client）。"""
        client = self._ha_client
        if client is None or not hasattr(client, "list_automations"):
            return []
        try:
            items = await client.list_automations()
        except Exception:  # noqa: BLE001
            logger.warning("HA automations list failed", exc_info=True)
            return []
        return [
            {
                "id": str(a.get("id", "")),
                "alias": str(a.get("alias", "") or "(未命名)"),
                "entity_id": str(a.get("entity_id", "")),
                "state": a.get("state", ""),
                "last_triggered": a.get("last_triggered"),
            }
            for a in items or []
        ]

    async def delete(self, automation_id: str) -> dict:
        client = self._ha_client
        if client is None or not hasattr(client, "delete_automation"):
            return {"error": "HA 客户端不可用"}
        try:
            await client.delete_automation(automation_id)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"删除失败: {exc}"}
        logger.info("HA automation deleted: %s", automation_id)
        # HA 2026 实测：config 删除后实体可能残留为 unavailable 幽灵
        # （config API 已 404 但实体还在），补一刀实体注册表移除。
        try:
            for a in await client.list_automations():
                if str(a.get("id")) == str(automation_id) and a.get("entity_id"):
                    if str(a.get("state")) in ("unavailable", "unknown"):
                        await client.remove_entity(str(a["entity_id"]))
                    break
        except Exception:  # noqa: BLE001
            logger.debug("ghost entity cleanup skipped", exc_info=True)
        return {"deleted": True, "id": automation_id}
