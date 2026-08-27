from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any

from .rule_registry_service import RuleRegistryService
from ..clients.client_factory import build_per_user_chat_client
from ..core.config import get_config

logger = logging.getLogger(__name__)

_EVAL_TIMEOUT_SECONDS = 60


class AutomationService:
    def __init__(
        self,
        rule_registry: RuleRegistryService,
        tool_executor=None,
        vision_service=None,
        ha_service=None,
    ) -> None:
        self._rule_registry = rule_registry
        self._tool_executor = tool_executor
        self._vision_service = vision_service
        # HA 状态快照提供者：设备状态门控用它查"动作目标态是否已满足"。
        self._ha_service = ha_service
        # 天气缓存：60s TTL，避免每次评估都请求外部 API
        self._weather_cache: dict | None = None
        self._weather_cache_at: float = 0.0
        # 缓存 chat LLM 客户端，避免每次规则评估都重新读取配置和解析 API key
        self._chat_client = None
        # per-user chat 客户端缓存：user_id → (key 签名, client)。
        # 每轮仍解析 key（单条 DB 查询，用于探测变更），签名不变则复用客户端，
        # 避免非视觉静默循环（默认 30s 一轮 × 每条规则）反复重建 httpx 连接池。
        self._per_user_clients: dict[str, tuple[tuple, Any]] = {}
        # CameraManager 引用（后注入）：虚拟摄像头演练开关（real_exec 标志）经它查询。
        self._camera_manager = None
        # 运行状态计数(按管道,含运动触发;/automation/status 展示用)。
        # agent 侧另有循环轮数计数,只含兜底不含运动触发,勿混用。
        self._vision_eval_count = 0
        self._context_eval_count = 0

    def set_camera_manager(self, cm) -> None:
        """注入 CameraManager（虚拟摄像头演练开关查询用）。"""
        self._camera_manager = cm

    async def evaluate(
        self,
        frames: list | None = None,
        camera_id: str = "",
        rule_types: tuple[str, ...] | None = None,
    ) -> list[dict]:
        """评估所有规则（async）——按 type 路由 + 设备状态门控。

        Task 5 多路化:camera_id 非空时,只评估绑定该摄像头 + 未绑定(camera_id='')
        的全局规则;camera_id 空串则评估所有(向后兼容单摄时代)。
        规则的 camera_id 非空且与传入 camera_id 不匹配 → 跳过。

        评估管道拆分:rule_types 限定本轮评估的规则类型(None=全部,向后兼容)。
        运动触发与视觉静默兜底传 ("vision",),非视觉静默循环传 ("time","weather")。

        路由（替代旧全局 use_context_only）：
          - type=time/weather → chat LLM（_evaluate_context_only，按时间+天气，无需帧）
          - type=vision        → VL（evaluate_condition，带 frames）；无帧则跳过该组
        设备状态门控（评估前）：动作蕴含目标态，cheap HA 查当前态，所有动作已在目标态
        → 跳过整条规则（0 LLM、0 action）。这是「窗帘保持关着 → 0 调用」的来源——设备
        已在目标态时不再调 LLM 复查条件。冷却只在动作真正执行后武装（update_trigger_time），
        与门控互补：冷却防瞬时重触，门控防稳态下重复评估。
        """
        applied: list[dict] = []
        now = time.time()
        rules = self._rule_registry.list_rules()
        # 管道计数:None=全部(兼容旧调用),计两侧
        counted_types = rule_types if rule_types is not None else ("vision", "time", "weather")
        if "vision" in counted_types:
            self._vision_eval_count += 1
        if "time" in counted_types or "weather" in counted_types:
            self._context_eval_count += 1

        # 一次性拉 HA 状态快照（5s 缓存，命中 0 网络），供设备门控复用
        state_map: dict[str, dict] = {}
        if self._ha_service is not None:
            try:
                snapshot = await self._ha_service.get_states_snapshot()
                state_map = {
                    str(s.get("entity_id")): s for s in snapshot if s.get("entity_id")
                }
            except Exception:
                logger.debug("HA states snapshot failed, device gate disabled this cycle", exc_info=True)

        # 分区：chat（time/weather）走 chat LLM，vision 走 VL；设备门控命中即跳过（0 LLM）
        chat_rules: list[dict] = []
        vl_rules: list[dict] = []
        gated_count = 0
        skipped_count = 0
        for rule in rules:
            # 管道过滤(最廉价的检查放最前):非本轮管道的类型直接跳过,
            # 如运动触发只评 vision;也避免他管道规则先命中设备门控污染 gated 归因。
            rtype = str(rule.get("type", "vision") or "vision").lower()
            if rule_types is not None and rtype not in rule_types:
                skipped_count += 1
                continue
            if not rule.get("enabled", True):
                logger.debug("Rule '%s' skipped: disabled", rule.get("name", ""))
                skipped_count += 1
                continue
            cooldown_left = self._cooldown_remaining(rule, now)
            if cooldown_left > 0:
                logger.debug("Rule '%s' skipped: in cooldown (%.1fs remaining)",
                           rule.get("name", ""), cooldown_left)
                skipped_count += 1
                continue
            condition_text = str(rule.get("condition", "")).strip()
            if not condition_text:
                logger.debug("Rule '%s' skipped: empty condition", rule.get("name", ""))
                skipped_count += 1
                continue  # 跳过无意义的空条件规则
            # Task 5:按摄像头过滤。规则 camera_id 非空时必须匹配;
            # 空串(未绑定)= 全局规则,归所有摄像头。camera_id='' 时评估全部(兼容)。
            rule_cam = str(rule.get("camera_id", "") or "")
            if camera_id and rule_cam and rule_cam != camera_id:
                skipped_count += 1
                continue
            # 设备状态门控：所有动作已在目标态 → 跳过（0 LLM、0 action）
            if self._device_already_in_target(rule, state_map):
                gated_count += 1
                logger.debug("Rule '%s' skipped: device already in target state", rule.get("name", ""))
                continue
            if rtype in ("time", "weather"):
                chat_rules.append(rule)
            else:  # vision / 未知 → VL
                vl_rules.append(rule)

        if not chat_rules and not vl_rules:
            logger.info("No eligible rules to evaluate (total=%d, skipped=%d, gated=%d)",
                        len(rules), skipped_count, gated_count)
            return applied

        context_info = await self._build_condition_context()
        has_vl = bool(frames) and self._vision_service is not None
        logger.info("Evaluating: %d chat-rule(s), %d vision-rule(s), %d frames, has_vl=%s (gated=%d)",
                    len(chat_rules), len(vl_rules), len(frames) if frames else 0, has_vl, gated_count)

        # chat 路由：time/weather 规则按时间+天气判（无帧也跑）
        if chat_rules:
            tasks = [
                self._evaluate_context_only(
                    str(r.get("condition", "")),
                    context_info,
                    str(r.get("user_id", "")),
                )
                for r in chat_rules
            ]
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_EVAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Chat rule eval timed out after %ds, skipping chat group", _EVAL_TIMEOUT_SECONDS)
            else:
                applied = await self._apply_results(chat_rules, results, now, applied, camera_id=camera_id)

        # vision 路由：VL 看画面，无帧/无 vision_service 则跳过该组
        if vl_rules:
            if not has_vl:
                logger.info("Skipping %d vision rule(s): no frames or vision service unavailable", len(vl_rules))
            else:
                # 一个 tick 编码一次，N 条规则复用同一份 b64（避免重复 imencode+base64）
                try:
                    pre_encoded_b64 = await self._vision_service.encode_frames_b64(frames)
                except Exception:
                    logger.warning("encode_frames_b64 failed, falling back to per-rule encoding", exc_info=True)
                    pre_encoded_b64 = None
                tasks = [
                    self._vision_service.evaluate_condition(
                        frames,
                        str(r.get("condition", "")),
                        context_info=context_info,
                        pre_encoded_b64=pre_encoded_b64,
                    )
                    for r in vl_rules
                ]
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_EVAL_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Vision rule eval timed out after %ds, skipping vision group", _EVAL_TIMEOUT_SECONDS)
                else:
                    applied = await self._apply_results(vl_rules, results, now, applied, camera_id=camera_id)

        if applied:
            logger.info("Automation rules applied", extra={"applied_count": len(applied)})
        return applied

    async def _apply_results(self, rules: list[dict], results: list, now: float,
                             applied: list[dict], camera_id: str = "") -> list[dict]:
        """统一处理评估结果：异常记日志跳过，result==1 执行动作 + 记指标 + 识别留痕。"""
        for rule, result in zip(rules, results):
            rule_id = rule.get("id", "")
            rule_name = rule.get("name", "")
            if isinstance(result, Exception):
                # result 来自 gather(return_exceptions=True)，此刻已不在 except 上下文，
                # traceback.format_exc() 会返回 "NoneType: None"（无活跃异常）。
                # 用异常对象自带的 __traceback__ 还原真实堆栈。
                tb_str = "".join(
                    traceback.format_exception(type(result), result, result.__traceback__)
                )
                logger.warning("Rule '%s' (id=%s) eval failed: %s\n%s",
                               rule_name, rule_id, result, tb_str)
                continue
            logger.info("Rule '%s' (id=%s) eval result: %s", rule_name, rule_id, result)
            # 识别留痕：模型对条件的判定（0/1）与条件原文
            await self._record_eval_log(camera_id, rule, result)
            # 记录自动化评估
            try:
                from ..container import get_container
                get_container().metrics_service.record_automation_eval()
            except Exception:
                pass
            if result == 1:
                applied.extend(await self._run_actions(rule, now, camera_id=camera_id))
                # 触发事件落 family_events（周报数据源），失败静默
                try:
                    from .alert_service import alert_service
                    n_actions = len(rule.get("actions", []) or [])
                    await alert_service.record(
                        "automation", f"rule:{rule_name}",
                        f"规则「{rule_name}」条件成立，执行了 {n_actions} 个动作")
                except Exception:  # noqa: BLE001
                    pass
        return applied

    async def _record_eval_log(self, camera_id: str, rule: dict, result) -> None:
        """规则条件判定留痕（vision_logs, kind=rule_eval）。失败静默。"""
        try:
            from ..core.database import Database
            await Database.get().vision_log_insert(camera_id, "rule_eval", {
                "rule_id": rule.get("id", ""),
                "rule_name": rule.get("name", ""),
                "condition": str(rule.get("condition", "")),
                "result": int(result) if isinstance(result, (int, float)) else str(result),
            })
        except Exception:  # noqa: BLE001
            logger.debug("rule eval log insert failed", exc_info=True)

    def _cooldown_remaining(self, rule: dict, now: float) -> float:
        """距冷却结束的剩余秒数（负数/0 = 已过冷却）。"""
        cooldown = int(rule.get("cooldown_seconds", get_config("automation.default_cooldown_seconds", 5)))
        last = float(rule.get("last_triggered_at", 0.0))
        return cooldown - (now - last)

    # ---------- 设备状态门控 ----------

    def _device_already_in_target(self, rule: dict, state_map: dict) -> bool:
        """规则所有动作的目标态是否都已满足（命中→评估前跳过，0 LLM、0 action）。

        保守策略：任一动作推导不出目标态、查不到当前状态、设备 unavailable/unknown
        → 返回 False（宁评估不漏，绝不误跳过）。无动作的规则也返回 False（保持原评估）。
        """
        actions = rule.get("actions") or []
        if not actions:
            return False
        for task in actions:
            tool_input = task.get("mcp_tool_input") or task.get("parameters") or {}
            domain = str(tool_input.get("domain", "") or "")
            service = str(tool_input.get("service", "") or "")
            entity_id = str(tool_input.get("entity_id", "") or "")
            data = tool_input.get("data") or {}
            if not entity_id or not service:
                return False  # 推导不出目标态
            target = self._derive_target_state(domain, service, data)
            if target is None:
                return False  # 不识别的 service（如 script/toggle/set_value）→ 不跳过
            current = state_map.get(entity_id)
            if current is None:
                return False  # 实体不存在/HA 不可达 → 不跳过
            if not self._matches_target_state(current, target):
                return False  # 不在目标态 → 需要执行
        return True

    def _derive_target_state(self, domain: str, service: str, data: dict) -> dict | None:
        """从动作 (domain, service, data) 推导目标态。

        返回 {state:...}（开/关类）或 {attributes:{attr:val,...}}（设值类）；无法确定 → None。
        覆盖常见 HA 服务；不识别的（toggle/script/set_value 等）返回 None（保守不跳过）。
        """
        if service == "turn_on":
            return {"state": "on"}
        if service == "turn_off":
            return {"state": "off"}
        if service == "open_cover":
            return {"state": "open"}
        if service == "close_cover":
            return {"state": "closed"}
        if service == "set_temperature" and "temperature" in data:
            return {"attributes": {"temperature": data["temperature"]}}
        if service == "set_humidity" and "humidity" in data:
            return {"attributes": {"humidity": data["humidity"]}}
        if service in ("set_cover_position", "set_position") and "position" in data:
            return {"attributes": {"current_position": data["position"]}}
        if service == "set_brightness" and "brightness" in data:
            return {"attributes": {"brightness": data["brightness"]}}
        return None

    def _matches_target_state(self, current: dict, target: dict) -> bool:
        """当前状态是否已满足目标态。unavailable/unknown 永远不算命中（保守）。

        开/关类比 state 字符串；设值类比 attributes，数值用 ±0.5 容忍（温控/亮度近似相等即
        视为已到位，避免 setpoint 26.0 vs 26 之类抖动导致反复触发）。
        """
        cur_state = str(current.get("state", ""))
        if cur_state in ("unavailable", "unknown", ""):
            return False
        if "state" in target:
            return cur_state == target["state"]
        if "attributes" in target:
            cur_attrs = current.get("attributes", {}) or {}
            for attr, want in target["attributes"].items():
                actual = cur_attrs.get(attr)
                if actual is None:
                    actual = cur_attrs.get(f"current_{attr}")
                if actual is None:
                    return False  # 缺属性 → 不跳过
                try:
                    if abs(float(actual) - float(want)) >= 0.5:
                        return False
                except (TypeError, ValueError):
                    if str(actual) != str(want):
                        return False
            return True
        return False

    def _virtual_dry_run(self, camera_id: str) -> bool:
        """该摄像头是否为演练模式：虚拟摄像头且未开真实执行开关。

        非虚拟摄像头恒 False（生产行为不变）；camera_manager 未注入恒 False。
        """
        if not camera_id or self._camera_manager is None:
            return False
        try:
            if not self._camera_manager.is_virtual_camera(camera_id):
                return False
            return not bool(self._camera_manager.get_virtual_flag(camera_id, "real_exec", False))
        except Exception:  # noqa: BLE001
            return False

    async def _run_actions(self, rule: dict, now: float, camera_id: str = "") -> list[dict]:
        """执行规则的所有动作，记录触发时间。"""
        out: list[dict] = []
        dry_run = self._virtual_dry_run(camera_id)
        for task in rule.get("actions", []):
            result = await self._execute_action(task, camera_id=camera_id, dry_run=dry_run)
            if result is not None:
                out.append({"rule": rule.get("name") or rule.get("summary") or rule.get("id"), "result": result})
        if out:
            self._rule_registry.update_trigger_time(rule["id"], now)
        return out

    async def _execute_action(self, task: dict, camera_id: str = "", dry_run: bool = False) -> dict | None:
        """通过 MCP 工具调用执行动作。dry_run=True 只记录不执行（虚拟摄像头演练）。"""
        tool_name = task.get("mcp_tool_name") or task.get("tool_name")
        if not tool_name:
            logger.warning("Action has no tool_name, skipping", extra={"task": task})
            return None
        tool_input = task.get("mcp_tool_input") or task.get("parameters") or {}
        if dry_run:
            logger.info("[演练] 虚拟摄像头 %s 规则命中，将执行: %s input: %s",
                        camera_id, tool_name, tool_input)
            await self._record_action_log(camera_id, tool_name, tool_input, attempted=False)
            return {"dry_run": True, "tool": tool_name, "input": tool_input}
        if self._tool_executor is None:
            logger.warning("Tool executor not available, cannot execute action", extra={"tool": tool_name})
            return None
        resolved = self._tool_executor.resolve_tool_name(str(tool_name))
        logger.info("Executing action: %s input: %s", resolved, tool_input)
        try:
            result = await self._tool_executor.execute_tool_by_name(resolved, dict(tool_input), None)
            # 记录工具调用
            try:
                from ..container import get_container
                get_container().metrics_service.record_tool_call(resolved)
            except Exception:
                pass
        except Exception:  # noqa: BLE001
            logger.exception("Rule MCP action failed", extra={"tool": resolved})
            # 记录工具调用错误
            try:
                from ..container import get_container
                get_container().metrics_service.record_tool_call(resolved, error=True)
            except Exception:
                pass
            await self._record_action_log(camera_id, resolved, tool_input, attempted=True, error="execution failed")
            return None
        if isinstance(result, dict) and result.get("success") is False:
            logger.warning("Rule MCP action returned failure", extra={"tool": resolved, "error": result.get("error")})
            await self._record_action_log(camera_id, resolved, tool_input, attempted=True,
                                          error=str(result.get("error", "")))
            return None
        logger.info("Action succeeded: %s", resolved)
        await self._record_action_log(camera_id, resolved, tool_input, attempted=True,
                                      result_summary=result if not isinstance(result, dict) else
                                      {k: result[k] for k in list(result)[:5]})
        return {"tool": resolved, "result": result}

    async def _record_action_log(self, camera_id: str, tool: str, tool_input: dict,
                                 attempted: bool, error: str = "", result_summary=None) -> None:
        """动作执行/演练留痕（vision_logs, kind=action）。失败静默。

        attempted：True=真实执行过（成败看 error/result 字段），False=演练（dry-run）。
        旧记录里的 executed/dry_run 双字段已合并为此单字段。
        """
        try:
            from ..core.database import Database
            content = {
                "tool": tool,
                "input": tool_input,
                "attempted": attempted,
            }
            if error:
                content["error"] = error
            if result_summary is not None:
                content["result"] = result_summary
            await Database.get().vision_log_insert(camera_id, "action", content)
        except Exception:  # noqa: BLE001
            logger.debug("action log insert failed", exc_info=True)

    async def _resolve_chat_client(self, user_id: str = ""):
        """按 user_id 解析 per-user chat client；无配置或 user_id 为空则回退全局 self._chat_client。

        per-user 客户端构造走 build_per_user_chat_client（强制 _enabled=True，绕过全局
        llm.enabled 占位符禁用态）。无 per-user 配置时 lazy init 全局 client 复用。

        per-user 客户端按 (api_key, base_url, model) 签名缓存：每轮仍解析 key
        探测变更（单条 DB 查询），签名不变则复用已构建的客户端与连接池。
        """
        if user_id:
            from ..core.key_resolver import resolve_key_for_role_user
            try:
                key_info = await resolve_key_for_role_user("chat", user_id)
            except Exception:
                key_info = None
            if key_info and key_info.get("api_key"):
                sig = (key_info.get("api_key"), key_info.get("base_url"), key_info.get("model"))
                cached = self._per_user_clients.get(user_id)
                if cached is not None and cached[0] == sig:
                    return cached[1]
                per_user = await build_per_user_chat_client("chat", user_id, force_enabled=True)
                if per_user is not None:
                    self._per_user_clients[user_id] = (sig, per_user)
                    return per_user
            else:
                # per-user 配置已删除：清缓存回退全局
                self._per_user_clients.pop(user_id, None)
        # 回退全局：lazy init 一次，后续复用
        if self._chat_client is None:
            from ..clients.llm_chat_client import LlmChatClient
            self._chat_client = LlmChatClient(role="chat")
        return self._chat_client

    async def _evaluate_context_only(self, condition: str, context: str, user_id: str = "") -> int:
        """用 chat LLM 根据时间+天气上下文判断条件是否成立。返回 0/1。

        user_id 非空时尝试用 per-user chat key；空（老规则）或解析失败时回退全局。
        """
        client = await self._resolve_chat_client(user_id)
        prompt = (
            f"当前环境信息：\n{context}\n\n"
            f"请判断以下条件是否成立，只回复 1（成立）或 0（不成立）：\n{condition}"
        )
        try:
            content = await client.chat([
                {"role": "system", "content": "你是一个条件判断器。只回复 1 或 0。"},
                {"role": "user", "content": prompt},
            ], 20)
            import re
            m = re.search(r'\d+', content.strip())
            if m:
                val = int(m.group())
                return 0 if val == 0 else 1
        except Exception:
            logger.warning("Context-only evaluation failed", exc_info=True)
        return 0

    async def _build_condition_context(self) -> str:
        """获取当前时间+天气，拼成简短上下文供 VL 模型判断条件。

        任何步骤失败静默降级，不阻塞评估。
        天气结果缓存 60s，避免频繁请求外部 API。
        """
        parts: list[str] = []

        # 时间：零成本，每次实时获取
        try:
            from ..mcp.local_mcp_servers import current_time_handler, _get_tz_offset_hours
            time_data = await current_time_handler({"tz_offset_hours": _get_tz_offset_hours()}, None)
            weekday = time_data.get("weekday", "")
            parts.append(
                f"当前时间：{time_data.get('date', '')} {weekday} {time_data.get('time', '')}"
            )
        except Exception:  # noqa: BLE001
            logger.debug("Failed to get time for condition context", exc_info=True)

        # 天气：60s 缓存
        now = time.time()
        if self._weather_cache is None or (now - self._weather_cache_at) >= 60:
            try:
                from ..mcp.weather_tools import get_weather_handler
                data = await get_weather_handler({}, None)
                if isinstance(data, dict) and "error" not in data:
                    self._weather_cache = data
                    self._weather_cache_at = now
            except Exception:  # noqa: BLE001
                logger.debug("Failed to get weather for condition context", exc_info=True)

        if self._weather_cache:
            from .weather_service import format_weather_brief
            weather_str = format_weather_brief(self._weather_cache)
            if weather_str:
                parts.append(f"天气：{weather_str}")

        return "\n".join(parts)
