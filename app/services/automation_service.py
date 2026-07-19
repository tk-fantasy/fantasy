from __future__ import annotations

import asyncio
import logging
import time
import traceback

from .rule_registry_service import RuleRegistryService

logger = logging.getLogger(__name__)

_EVAL_TIMEOUT_SECONDS = 60


class AutomationService:
    def __init__(
        self,
        rule_registry: RuleRegistryService,
        tool_executor=None,
        vision_service=None,
    ) -> None:
        self._rule_registry = rule_registry
        self._tool_executor = tool_executor
        self._vision_service = vision_service
        # 天气缓存：60s TTL，避免每次评估都请求外部 API
        self._weather_cache: dict | None = None
        self._weather_cache_at: float = 0.0
        # 缓存 chat LLM 客户端，避免每次规则评估都重新读取配置和解析 API key
        self._chat_client = None

    async def evaluate(self, frames: list | None = None) -> list[dict]:
        """评估所有视觉规则(async)。

        只处理有 condition 的视觉规则，用 VL 模型并行评估:
          - 输出 0: 条件不成立，跳过
          - 输出 1: 条件成立，执行动作

        保留 cooldown 防重复触发和动作执行逻辑。
        """
        applied: list[dict] = []
        now = time.time()
        rules = self._rule_registry.list_rules()

        # 收集有 condition 的视觉规则
        vision_rules: list[dict] = []
        skipped_count = 0
        for rule in rules:
            if not rule.get("enabled", True):
                logger.debug("Rule '%s' skipped: disabled", rule.get("name", ""))
                skipped_count += 1
                continue
            if self._in_cooldown(rule, now):
                cooldown = int(rule.get("cooldown_seconds", 10))
                last = float(rule.get("last_triggered_at", 0.0))
                logger.debug("Rule '%s' skipped: in cooldown (%.1fs remaining)",
                           rule.get("name", ""), cooldown - (now - last))
                skipped_count += 1
                continue
            condition_text = str(rule.get("condition", "")).strip()
            if not condition_text:
                logger.debug("Rule '%s' skipped: empty condition", rule.get("name", ""))
                skipped_count += 1
                continue  # 跳过无意义的空条件规则
            vision_rules.append(rule)

        if not vision_rules:
            logger.info("No eligible rules to evaluate (total=%d, skipped=%d)", len(rules), skipped_count)
            return applied

        # 没有 frames 时，用纯上下文（时间+天气）评估条件
        use_context_only = not frames or self._vision_service is None
        if use_context_only:
            logger.info("No camera frames, evaluating %d rules with context (time + weather)", len(vision_rules))

        logger.info("Evaluating %d rules with %d frames", len(vision_rules), len(frames) if frames else 0)

        # 获取环境上下文（时间+天气），所有规则共享
        context_info = await self._build_condition_context()

        if use_context_only:
            # 纯上下文评估：用 chat LLM 根据时间+天气判断条件（并发评估所有规则）
            # 每条规则按其 user_id 解析 per-user chat client（老规则 user_id='' 回退全局）
            eval_tasks = [
                self._evaluate_context_only(
                    str(rule.get("condition", "")),
                    context_info,
                    str(rule.get("user_id", "")),
                )
                for rule in vision_rules
            ]
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*eval_tasks, return_exceptions=True),
                    timeout=_EVAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Rule evaluation timed out after %ds, skipping this cycle", _EVAL_TIMEOUT_SECONDS)
                return applied

            for rule, result in zip(vision_rules, results):
                rule_name = rule.get("name", "")
                if isinstance(result, Exception):
                    logger.warning("Context eval failed for '%s': %s", rule_name, result)
                    continue
                logger.info("Rule '%s' context eval result: %s", rule_name, result)
                # 记录自动化评估
                try:
                    from ..container import get_container
                    get_container().metrics_service.record_automation_eval()
                except Exception:
                    pass
                if result == 1:
                    applied_rule = await self._run_actions(rule, now)
                    applied.extend(applied_rule)
        else:
            # 视觉评估：用 VL 模型 + 摄像机帧
            # 一个 tick 编码一次，N 条规则复用同一份 b64（避免重复 imencode+base64）
            try:
                pre_encoded_b64 = await self._vision_service.encode_frames_b64(frames)
            except Exception:
                logger.warning("encode_frames_b64 failed, falling back to per-rule encoding", exc_info=True)
                pre_encoded_b64 = None

            tasks = []
            for rule in vision_rules:
                tasks.append(
                    self._vision_service.evaluate_condition(
                        frames,
                        str(rule.get("condition", "")),
                        context_info=context_info,
                        pre_encoded_b64=pre_encoded_b64,
                    )
                )

            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_EVAL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Rule evaluation timed out after %ds, skipping this cycle", _EVAL_TIMEOUT_SECONDS)
                return applied

            for rule, result in zip(vision_rules, results):
                rule_id = rule.get("id", "")
                rule_name = rule.get("name", "")
                if isinstance(result, Exception):
                    logger.warning("Condition eval failed for '%s': %s\n%s",
                                 rule_name, result, traceback.format_exc())
                    continue

                logger.info("Rule '%s' (id=%s) evaluation result: %s",
                           rule_name, rule_id, result)

                # 记录自动化评估
                try:
                    from ..container import get_container
                    get_container().metrics_service.record_automation_eval()
                except Exception:
                    pass

                if result == 1:
                    rule_applied = await self._run_actions(rule, now)
                    applied.extend(rule_applied)

        if applied:
            logger.info("Automation rules applied", extra={"applied_count": len(applied)})
        return applied

    def _in_cooldown(self, rule: dict, now: float) -> bool:
        cooldown = int(rule.get("cooldown_seconds", 10))
        last = float(rule.get("last_triggered_at", 0.0))
        return (now - last) < cooldown

    async def _run_actions(self, rule: dict, now: float) -> list[dict]:
        """执行规则的所有动作，记录触发时间。"""
        out: list[dict] = []
        for task in rule.get("actions", []):
            result = await self._execute_action(task)
            if result is not None:
                out.append({"rule": rule.get("name") or rule.get("summary") or rule.get("id"), "result": result})
        if out:
            self._rule_registry.update_trigger_time(rule["id"], now)
        return out

    async def _execute_action(self, task: dict) -> dict | None:
        """通过 MCP 工具调用执行动作。"""
        tool_name = task.get("mcp_tool_name") or task.get("tool_name")
        if not tool_name:
            logger.warning("Action has no tool_name, skipping", extra={"task": task})
            return None
        if self._tool_executor is None:
            logger.warning("Tool executor not available, cannot execute action", extra={"tool": tool_name})
            return None
        tool_input = task.get("mcp_tool_input") or task.get("parameters") or {}
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
            return None
        if isinstance(result, dict) and result.get("success") is False:
            logger.warning("Rule MCP action returned failure", extra={"tool": resolved, "error": result.get("error")})
            return None
        logger.info("Action succeeded: %s", resolved)
        return {"tool": resolved, "result": result}

    async def _resolve_chat_client(self, user_id: str = ""):
        """按 user_id 解析 per-user chat client；无配置或 user_id 为空则回退全局 self._chat_client。

        与 scheduler_service._resolve_reminder_client 同一模式：
        resolve_key_for_role_user 拿到 per-user key → 构造独立 LlmChatClient，
        覆盖 _api_key/_base_url/_model/_enabled=True（绕过全局 llm.enabled 占位符禁用态）。
        老规则 user_id='' 直接走全局，保持原行为。
        """
        if user_id:
            try:
                from ..core.key_resolver import resolve_key_for_role_user
                key_info = await resolve_key_for_role_user("chat", user_id)
                if key_info and key_info.get("api_key"):
                    from ..clients.llm_chat_client import LlmChatClient
                    client = LlmChatClient(role="chat")
                    client._api_key = key_info["api_key"]
                    client._base_url = key_info["base_url"]
                    client._model = key_info["model"]
                    client._enabled = True
                    return client
            except Exception:
                logger.debug("Failed to resolve per-user automation chat client, using global", exc_info=True)
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

