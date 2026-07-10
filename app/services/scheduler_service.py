"""定时任务调度器 — 到点触发，执行复用现有出口。

与 AutomationAgent（视觉规则 + LLM 语义判断）互补：
- 自动化 = 模糊条件 + LLM 判断，靠轮询
- 定时任务 = 精确时刻 + 零 LLM 开销，事件驱动

触发类型 v1：at（一次性时刻）/ every（固定间隔）/ cron（标准表达式）。
payload 三种，都复用已有出口：
- kind=tool  → ToolExecutor.execute_tool_by_name（与 AutomationService._execute_action 同链路）
- kind=message → Dispatcher.dispatch（往主会话发一句固定文本，与 WS 聊天同入口）
- kind=reminder → Dispatcher.dispatch（把用户原始意图作为"提醒触发指令"投递，由 AI 主动组织语言提醒，而非冒充用户发言）

持久化：SQLite scheduled_tasks 表，配置 + 运行时状态合并存 JSON（单用户本地助手，
量级小，不需要 OpenClaw 那套配置列/运行时列分离优化）。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from croniter import croniter

from ..core.database import Database
from ..core.tracing import new_request_id
from ..schema.chat_schema import Event, Nlp

logger = logging.getLogger(__name__)

# tick 间隔：1 秒扫一次 due 任务。太短浪费 CPU，太长影响 at 类型精度。
_TICK_INTERVAL_SECONDS = 1.0


def compute_next_run(schedule: dict, now_ts: float) -> float | None:
    """根据 schedule 计算下一次触发的 Unix 时间戳（秒）。无下次返回 None。

    Args:
        schedule: {kind: "at"|"every"|"cron", ...}
            - at:   {kind:"at", at:"2026-07-07T08:00:00"}  ISO 字符串，一次性
            - every:{kind:"every", every_seconds: 3600}
            - cron: {kind:"cron", expr:"0 8 * * *"}
        now_ts: 当前 Unix 时间戳（秒）
    """
    kind = schedule.get("kind")
    if kind == "at":
        at_str = str(schedule.get("at", ""))
        if not at_str:
            return None
        try:
            # 兼容带/不带毫秒、带 Z 的 ISO 字符串
            dt = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
            ts = dt.timestamp()
            return ts if ts > now_ts else None
        except (ValueError, TypeError):
            logger.warning("scheduler: invalid 'at' value: %s", at_str)
            return None
    if kind == "every":
        secs = float(schedule.get("every_seconds", 0) or 0)
        if secs <= 0:
            return None
        return now_ts + secs
    if kind == "cron":
        expr = str(schedule.get("expr", ""))
        if not expr:
            return None
        try:
            now_dt = datetime.fromtimestamp(now_ts)
            nxt = croniter(expr, now_dt).get_next(datetime)
            return nxt.timestamp()
        except Exception:
            logger.warning("scheduler: invalid cron expr: %s", expr)
            return None
    logger.warning("scheduler: unknown schedule kind: %s", kind)
    return None


def summarize_schedule(schedule: dict) -> str:
    """人类可读的触发摘要，供前端展示与日志。"""
    kind = schedule.get("kind")
    if kind == "at":
        return f"于 {schedule.get('at', '?')} 执行一次"
    if kind == "every":
        secs = float(schedule.get("every_seconds", 0) or 0)
        if secs >= 3600:
            return f"每 {secs / 3600:.1f} 小时"
        if secs >= 60:
            return f"每 {secs / 60:.1f} 分钟"
        return f"每 {secs:.0f} 秒"
    if kind == "cron":
        return f"cron: {schedule.get('expr', '?')}"
    return "未知触发"


class SchedulerService:
    """定时任务调度器 — 到点触发，执行复用现有出口。

    依赖通过构造注入；dispatcher 用 list[0] ref 模式支持运行时热替换
    （与 AppContainer.automation_agent_ref 同模式）。
    """

    def __init__(
        self,
        db: Database,
        tool_executor: Any,
        dispatcher_ref: list,  # [Dispatcher | None] — 热替换
        session_store: Any,
        task_manager: Any = None,  # TaskManager，可选（执行任务时用它 spawn）
        llm_chat_client: Any = None,  # LlmChatClient — reminder kind 直接调 LLM，绕开 ReAct
    ) -> None:
        self._db = db
        self._tool_executor = tool_executor
        self._dispatcher_ref = dispatcher_ref
        self._session_store = session_store
        self._task_manager = task_manager
        self._llm_chat_client = llm_chat_client
        # 内存任务表：id -> task dict。读多写少，启动时从 DB 载入，CRUD 同步两边。
        self._tasks: dict[str, dict] = {}
        self._tick_task: asyncio.Task | None = None
        self._running = False
        # 单任务执行锁：同一 task 不会并发执行（防止 every 周期短于执行时长时重叠）
        self._executing: set[str] = set()
        # fallback 路径（无 task_manager 时）创建的后台任务引用，防止被 GC 中途回收
        self._background_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动调度器：从 DB 载入任务，启动 tick 循环。"""
        await self._load_tasks()
        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop(), name="scheduler-tick")
        logger.info("SchedulerService started (%d tasks loaded)", len(self._tasks))

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        logger.info("SchedulerService stopped")

    async def _load_tasks(self) -> None:
        """从 DB 载入全部任务到内存。"""
        rows = await self._db.scheduled_tasks_all()
        self._tasks = {t["id"]: t for t in rows}
        # 启动时重算所有启用任务的 next_run（上次崩溃遗留的 running 状态清掉）
        now = time.time()
        for task in self._tasks.values():
            if task.get("enabled", True):
                task["last_status"] = task.get("last_status")  # 保留历史
                # 重启恢复：若上次正在执行（running），标记为 interrupted
                if task.get("last_status") == "running":
                    task["last_status"] = "interrupted"
                    task["last_error"] = "进程重启，上次执行被中断"
                task["next_run_at"] = compute_next_run(task.get("schedule", {}), now)
            else:
                task["next_run_at"] = None
            await self._db.scheduled_task_update(task["id"], task)

    # ------------------------------------------------------------------
    # tick 循环
    # ------------------------------------------------------------------

    async def _tick_loop(self) -> None:
        """每秒扫一次 due 任务。"""
        while self._running:
            try:
                await self._tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(_TICK_INTERVAL_SECONDS)

    async def _tick_once(self) -> None:
        """单次扫描：找出 due 且 enabled 且未在执行的任务，spawn 执行。"""
        now = time.time()
        due: list[dict] = []
        for task in self._tasks.values():
            if not task.get("enabled", True):
                continue
            if task["id"] in self._executing:
                continue
            nxt = task.get("next_run_at")
            if nxt is not None and nxt <= now:
                due.append(task)

        for task in due:
            self._executing.add(task["id"])
            if self._task_manager is not None:
                self._task_manager.spawn(self._execute_task(task), name=f"scheduled-{task['id']}")
            else:
                self._spawn_background(self._execute_task(task))

    def _spawn_background(self, coro) -> None:
        """无 task_manager 时的 fallback：创建后台任务并保留引用防 GC。

        与 utils.async_utils.TaskManager.spawn 行为对齐：任务完成后自动从集合移除。
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def _execute_task(self, task: dict) -> None:
        """执行单个任务，按 payload.kind 分发到现有出口。"""
        task_id = task["id"]
        name = task.get("name", task_id)
        payload = task.get("payload", {})
        kind = payload.get("kind")
        now = time.time()

        # 标记开始执行
        task["last_status"] = "running"
        task["last_run_at"] = now
        task["last_error"] = ""
        await self._db.scheduled_task_update(task_id, task)

        try:
            if kind == "tool":
                await self._execute_tool_payload(payload)
            elif kind == "message":
                await self._execute_message_payload(payload, name)
            elif kind == "reminder":
                await self._execute_reminder_payload(payload, name)
            else:
                raise ValueError(f"unknown payload kind: {kind}")

            task["last_status"] = "success"
            logger.info("scheduled task '%s' (%s) succeeded", name, task_id)
        except Exception as exc:
            task["last_status"] = "failed"
            task["last_error"] = str(exc)
            logger.warning("scheduled task '%s' (%s) failed: %s", name, task_id, exc)
        finally:
            # at 类型一次性任务：成功/失败后禁用（不自动重试，下次到点自然不再触发）
            schedule = task.get("schedule", {})
            if schedule.get("kind") == "at":
                task["enabled"] = False
                task["next_run_at"] = None
            else:
                # 周期任务：算下次触发
                task["next_run_at"] = compute_next_run(schedule, time.time())
            await self._db.scheduled_task_update(task_id, task)
            self._executing.discard(task_id)

    async def _execute_tool_payload(self, payload: dict) -> None:
        """执行 tool payload：复用 ToolExecutor 链路（与 AutomationService._execute_action 同出口）。"""
        tool_name = str(payload.get("tool_name", ""))
        tool_input = payload.get("tool_input") or {}
        if not tool_name:
            raise ValueError("payload.tool_name is required")
        resolved = self._tool_executor.resolve_tool_name(tool_name)
        result = await self._tool_executor.execute_tool_by_name(resolved, dict(tool_input), None)
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(f"tool {resolved} returned failure: {result.get('error', result)}")
        logger.info("scheduled tool %s result: %s", resolved, result.get("result"))

    async def _execute_message_payload(self, payload: dict, task_name: str) -> None:
        """执行 message payload：往主会话发一句，复用 Dispatcher.dispatch。

        不新建独立 agent 会话——就是定时器替你"说一句话"进对话流，
        结果自然进会话历史（与 WS 聊天走同一入口）。
        """
        message = str(payload.get("message", "")).strip()
        if not message:
            raise ValueError("payload.message is required")

        dispatcher = self._dispatcher_ref[0]
        if dispatcher is None:
            raise RuntimeError("dispatcher not available")

        session_id = await self._resolve_main_session_id()
        if session_id is None:
            raise RuntimeError("无可用会话，无法投递定时消息（请先在聊天页创建会话）")

        rid = new_request_id()
        event = Event.build_event(
            Nlp.Request(query=message),
            request_id=rid,
            session_id=session_id,
        )
        logger.info("scheduled message task '%s' -> session %s: %s", task_name, session_id, message[:120])
        instructions = await dispatcher.dispatch(event)
        logger.info("scheduled message task '%s' dispatched, %d instructions", task_name, len(instructions))

    async def _execute_reminder_payload(self, payload: dict, task_name: str) -> None:
        """执行 reminder payload：直接调 LLM 生成一句提醒，写进主会话。

        与 message 的核心区别：
        - message 把存死文本当成用户发言注入 dispatch，AI 被动回复，
          还可能触发 ReAct 调工具/建新任务（用户看到的"下班时间到了"→AI 又建任务）；
        - reminder 绕开 ReAct，直接拿 session 历史 + system prompt + 提醒指令，
          让 LLM 生成一句话作为 assistant 回复存进会话——
          AI 主动开口、不碰任何工具、不可能创建新任务。

        payload 字段：
        - intent: 提醒意图简述（如"下班提醒"）
        - original: 用户创建时的原话（如"在18点27分提醒我下班"），优先用这个
        """
        intent = str(payload.get("intent", "")).strip()
        original = str(payload.get("original", "")).strip()
        source = original or intent
        if not source:
            raise ValueError("payload.intent 或 payload.original 至少填一个")

        if self._llm_chat_client is None:
            raise RuntimeError("llm_chat_client 未注入，无法执行 reminder（检查 scheduler 装配）")

        session_id = await self._resolve_main_session_id()
        if session_id is None:
            raise RuntimeError("无可用会话，无法投递定时提醒（请先在聊天页创建会话）")

        # 取主会话，构建带历史上下文的消息列表
        session = await self._session_store.get_or_create(session_id, new_request_id())

        from .prompt_service import build_system_prompt
        system_prompt = await build_system_prompt(query=source)

        now_str = datetime.now().strftime("%H:%M")
        directive = (
            f"[定时提醒触发] 用户创建此任务时说：「{source}」。"
            f"现在是 {now_str}，已到预定提醒时刻。"
            f"请你作为助手主动提醒用户——语气自然简短，就像你主动开口提醒一样。"
            f"只输出提醒这一句话，不要调用任何工具，不要创建新任务。"
        )

        # 拼消息：system + 历史 + 提醒指令（OpenAI dict 格式，llm_chat_client.chat 直接吃）
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(session.model_messages)
        messages.append({"role": "user", "content": directive})

        reply = await self._llm_chat_client.chat(messages, timeout=60)
        reply = str(reply).strip()
        if not reply:
            raise RuntimeError("LLM 返回空回复，reminder 未生成提醒文本")

        # 把提醒指令 + AI 回复写进会话历史（后续对话有上下文）
        session.model_messages.append({"role": "user", "content": directive})
        session.model_messages.append({"role": "assistant", "content": reply})
        await self._session_store.store_session(session)

        logger.info("scheduled reminder task '%s' -> session %s, reply: %s",
                    task_name, session_id, reply[:120])

    async def _resolve_main_session_id(self) -> str | None:
        """取最近更新的会话作为主会话；无则返回 None。

        定时消息任务需要一个会话来承接——取最近活跃的会话最符合直觉
        （用户最后聊的那个窗口）。若没有任何会话，跳过并记日志。
        """
        summaries = await self._session_store.list_summaries()
        if not summaries:
            return None
        # list_summaries 已按 updated_at 倒序，第一个就是最近活跃的
        return summaries[0].get("id")

    # ------------------------------------------------------------------
    # CRUD（写 DB + 更新内存 + 重算 next_run）
    # ------------------------------------------------------------------

    async def add_task(self, task: dict) -> dict:
        """新建定时任务。补全 id/时间戳/next_run，写 DB + 内存。"""
        task_id = task.get("id") or str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        now = time.time()
        task.setdefault("name", "未命名任务")
        task.setdefault("enabled", True)
        task["id"] = task_id
        task["created_at"] = now_ms
        task["updated_at"] = now_ms
        task["last_status"] = ""
        task["last_error"] = ""
        task["last_run_at"] = None
        task["next_run_at"] = compute_next_run(task.get("schedule", {}), now) if task.get("enabled", True) else None

        await self._db.scheduled_task_insert(task_id, task)
        self._tasks[task_id] = task
        logger.info("scheduled task added: '%s' (%s) -> %s",
                    task.get("name"), task_id, summarize_schedule(task.get("schedule", {})))
        return task

    async def update_task(self, task_id: str, patch: dict) -> dict | None:
        """部分更新任务。schedule/enabled 改动会重算 next_run。"""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        # 合并 patch（不覆盖 id/created_at）
        for k, v in patch.items():
            if k in ("id", "created_at"):
                continue
            task[k] = v
        task["updated_at"] = int(time.time() * 1000)
        # schedule 或 enabled 改动 → 重算 next_run
        if "schedule" in patch or "enabled" in patch:
            task["next_run_at"] = compute_next_run(task.get("schedule", {}), time.time()) if task.get("enabled", True) else None
        await self._db.scheduled_task_update(task_id, task)
        return task

    async def delete_task(self, task_id: str) -> bool:
        """删除任务。"""
        existed = task_id in self._tasks
        if existed:
            self._tasks.pop(task_id, None)
        await self._db.scheduled_task_delete(task_id)
        return existed

    async def list_tasks(self) -> list[dict]:
        """列出全部任务（按创建时间正序）。"""
        return sorted(self._tasks.values(), key=lambda t: t.get("created_at", 0))

    async def get_task(self, task_id: str) -> dict | None:
        return self._tasks.get(task_id)

    async def set_enabled(self, task_id: str, enabled: bool) -> dict | None:
        """启停任务。启用时重算 next_run。"""
        return await self.update_task(task_id, {"enabled": enabled})

    async def run_now(self, task_id: str) -> dict | None:
        """手动触发一次（不等 schedule）。用于调试。"""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if task_id in self._executing:
            return task  # 正在执行，跳过
        self._executing.add(task_id)
        if self._task_manager is not None:
            self._task_manager.spawn(self._execute_task(task), name=f"scheduled-manual-{task_id}")
        else:
            self._spawn_background(self._execute_task(task))
        return task
