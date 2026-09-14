"""校验 Agent — 模型行动性表态的三级核查流水线。

设计（讨论定稿）：
模型只要"要做事"，回复里必然留下三时态表态之一——已完成（已打开）、
进行中（正在关闭）、将来时（我将帮你关闭）。这些表态本身就是意图信号，
不需要额外的 LLM 意图分析调用。核查流水线全部由代码驱动：

  1. 宽覆盖正则命中表态（三时态 + 控制动词，误报无害——下游实体核对兜底）
  2. 从表态上下文提取设备指称，对 entity_name_map 模糊匹配：
     - 匹配到实体 + 将要时态 + 0 次工具调用 → 确定性撒谎，直接重试（零 LLM）
     - 匹配到实体 + 已/正在时态 → 读 HA 真实状态核对：
       相符 → 静默通过（灯本来就开着，不再瞎重试）；
       不符 → 定向重试，消息里注入真实状态（比"你必须调工具"服从率高）；
       状态语义不可核（调温/媒体等）或读不到 → 退回通用强制重试（旧行为）
     - 未匹配到实体：
       - 断言或 query 含设备名词 → 幻觉信号，重试让模型老实查 get_entities
       - 都没有 → 闲聊（"方案已经设置好了"），静默通过——顺带修掉旧版
         硬规则对这类句式的盲目重试误伤
  3. 正则未命中表态：query 带控制意图才花一次 LLM 语义校验兜底（正则漏网
     的非常规句式）；纯闲聊零调用——旧版这里每轮闲聊都白付一次 LLM 调用

chat_assistant.claim_verify_enabled=False 整体回退旧行为（硬规则 + 无条件
LLM 校验），与 rag.summary_trim_enabled 同款的可回退开关风格。
"""
from __future__ import annotations

import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..core.config import get_config
from ..core.key_resolver import resolve_key_for_role

logger = logging.getLogger(__name__)

# 匹配"声称已完成设备控制操作"的措辞，用于硬规则短路（旧路径保留）：
# tool_call_count==0 但说了这类话 → 模型在撒谎，强制重试。
#
# 设计原则：只匹配"已+控制动词"这种强完成态结构，不匹配通用完成词。
# - "已打开/已关闭/已调节/已切换" 基本只出现在设备控制语境，闲聊不会这么说；
# - 刻意不收 "完成/搞定/好了" 等通用词——它们在闲聊里太常见
#   （"计划好了""方案完成了"），会误判正常对话为撒谎。
# - 动作动词是通用控制动作（开/关/调/设置/切换），不硬编码设备名，
#   用户加新设备类型无需改正则。
_ACTION_DONE_RE = re.compile(
    r"已(经)?(打开|关闭|开启|关掉|调节|调整|设置|切换)|"
    r"帮\s*你.*(打开|关闭|开启|关掉|调[节整])",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# 三时态行动性表态检测（宽覆盖：误报由下游实体核对过滤，不直接触发重试）
# ---------------------------------------------------------------------------

# 控制动词表：多词在前保证交替优先匹配长词（"打开"不被"开"抢走）。
_ACTION_VERBS = (
    "打开|开启|开好|关闭|关掉|关上|关好|调大|调小|调高|调低|调节|调整|调好"
    "|设置|切换|拉开|拉上|升起|降下|暂停|播放|停止|锁定|解锁|启动|熄灭|点亮"
    "|开|关|调"
)
_VERB_G1 = "(?P<v1>" + _ACTION_VERBS + ")"
_VERB_G2 = "(?P<v2>" + _ACTION_VERBS + ")"
_VERB_G3 = "(?P<v3>" + _ACTION_VERBS + ")"

# 已完成时态：已(经)…打开 / 帮你打开了 / 打开了 / 关好了 / 关掉了。
# 第二/三分支的完成助词（了/好/完/啦/掉）是必需项——否则"帮你关闭"
# （将来时意图）会被判成完成态，"开心""空调"这类词里的单字开/关/调
# 会把一切闲聊都判成表态。
_DONE_CLAIM_RE = re.compile(
    r"已(?:经)?[^。！？,，；;]{0,4}?" + _VERB_G1
    + r"|[帮给]\s*[我你][^。！？,，；;]{0,8}?" + _VERB_G2 + r"(?:了|好|完|啦|掉)"
    + r"|" + _VERB_G3 + r"(?:了|好|完|啦|掉)(?:哦|啦|咯)?"
)
# 进行中时态：正在…关闭 / 这就…开
_DOING_CLAIM_RE = re.compile(r"(?:正在|这就)[^。！？,，；;]{0,6}?" + _VERB_G3)
# 将来时态：将(要)…关闭 / 我来…调 / 马上(就)…开
_WILL_CLAIM_RE = re.compile(
    r"(?:将(?:要)?|我会|我要|我来|待会|稍后|马上(?:就)?|立刻|准备)"
    r"[^。！？,，；;]{0,6}?" + _VERB_G3
)

# 规则创建声明（创建关键词门控的配套核查）：回复声称"已创建规则/创建成功"。
# 门控下若本轮没有调过 automation_rule_create（无关键词变体里该工具根本不存在，
# 或工具层拒绝），这类声明必为幻觉。完成态框架参照 _DONE_CLAIM_RE：必须有
# 已/成功/了 等完成助词，避免把「要创建规则可以说…」这类教学句误判。
_RULE_CREATE_CLAIM_RE = re.compile(
    r"已(?:经)?[^。！？,，；;]{0,8}?(?:创建|建立|新建|添加)(?:了)?"
    r"|(?:创建|建立|新建|添加)(?:了|好|成功)[^。！？]{0,16}规则"
    r"|(?:创建|建立|新建|添加)(?:了)?[^。！？]{0,10}规则[^。！？]{0,6}(?:成功|好了?)"
    r"|规则[^。！？]{0,4}(?:创建|建立|新建|添加)(?:了)?(?:成功|好了?)"
)
# 泄漏形态：无创建权限的回合里，模型把创建工具调用当正文输出（打印工具名 + 参数
# JSON）。工具名出现在回复里本身就是泄漏的铁证（本轮它不该知道这个工具）。
_RULE_TOOL_LEAK_RE = re.compile(r"automation_rule_create")

# 设备名词表：断言未匹配到实体时，名词出现说明说的是设备（幻觉信号），
# 一个名词都没有更像闲聊（"方案已经设置好了"）。
_DEVICE_NOUN_RE = re.compile(
    r"灯|空调|窗帘|窗|摄像头|门锁|门|插座|开关|电视|音响|音箱|风扇|加湿"
    r"|除湿|净化|新风|热水器|地暖|温控|传感|报警|晾衣|扫地|阀门|水泵"
    r"|场景|投影|路由|门铃"
)

# 用户 query 的控制意图信号（比断言正则更宽：只用于决定"要不要花 LLM
# 校验/重试"，误命中只是多一次校验，等于旧行为；漏命中才会漏检）。
_CONTROL_INTENT_RE = re.compile(
    _ACTION_VERBS
    + r"|灯|空调|窗帘|窗|摄像头|门锁|门|插座|电视|音响|音箱|风扇|加湿|除湿"
    r"|净化|新风|热水器|地暖|温控|传感|扫地|场景|投影"
    r"|看看|看一眼|查|检查|是否|有没有|多少度|几度|状态|画面"
    r"|定时|提醒|每天|明天|后天|分钟后|小时后|帮我|给我|麻烦|把|让"
)

# 表态里提取宾语时剔掉的助词/虚词（只清对象文本，不动实体名）
_OBJ_STRIP_CHARS = set("的了啦哦咯呢吧呀啊哈把给都也就还在是已经将要立刻马上准备被我让他")
_OBJ_PUNCT_RE = re.compile(r"[，。！？!?,.;；:：~～、'\"“”‘’()（）\[\]【】\s]")

# 开关类状态可核对的域（白名单制：陌生域的 state 语义不可靠，不核）
_ONOFF_DOMAINS = {"light", "switch", "fan", "humidifier", "dehumidifier", "input_boolean", "siren"}
_ON_VERBS = {"打开", "开启", "开好", "开", "点亮", "启动"}
_OFF_VERBS = {"关闭", "关掉", "关上", "关好", "关", "熄灭", "停止"}


def _expected_state(verb: str, entity_id: str) -> str | None:
    """断言动词 + 实体域 → 期望的 HA state 值。不可核对返回 None。"""
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain == "cover":
        if verb in _ON_VERBS or verb in {"拉开", "升起"}:
            return "open"
        if verb in _OFF_VERBS or verb in {"拉上", "降下"}:
            return "closed"
        return None
    if domain not in _ONOFF_DOMAINS:
        return None
    if verb in _ON_VERBS:
        return "on"
    if verb in _OFF_VERBS:
        return "off"
    return None


def _clean_obj_text(text: str) -> str:
    """宾语候选文本清洗：去标点/空白/助词虚词，剩核心名词便于包含匹配。"""
    return "".join(ch for ch in _OBJ_PUNCT_RE.sub("", text) if ch not in _OBJ_STRIP_CHARS)


def _claim_contexts(text: str, m: re.Match) -> tuple[str, str]:
    """取表态命中位置前后的小窗口作宾语候选（中文两种语序都出现：
    「客厅灯已打开」宾语在前，「已打开客厅灯」宾语在后）。"""
    before = text[max(0, m.start() - 8):m.start()]
    after = text[m.end():m.end() + 10]
    after_parts = _OBJ_PUNCT_RE.split(after)
    before_parts = _OBJ_PUNCT_RE.split(before)
    return (before_parts[-1] if before_parts else "", after_parts[0] if after_parts else "")


def _match_entity(obj_text: str, entity_name_map: dict[str, str] | None) -> tuple[str, str] | None:
    """宾语文本对 {entity_id: friendly_name} 做双向包含模糊匹配。

    取名字最长的命中（更具体）。宾语清洗后不足 2 字不匹配——单字"灯"
    会碰上家里任何一盏灯，核对到错误设备比不核对更糟。
    """
    o = _clean_obj_text(obj_text)
    if len(o) < 2:
        return None
    best: tuple[str, str] | None = None
    for eid, name in (entity_name_map or {}).items():
        n = _clean_obj_text(str(name))
        if len(n) < 2:
            continue
        if o in n or n in o:
            if best is None or len(n) > len(best[1]):
                best = (str(eid), str(name))
    return best


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

_VALIDATOR_SYSTEM_PROMPT = (
    "你是一个校验助手。你的唯一任务是判断一段对话回复是否只表达了要做某事的意图，"
    "但没有确认动作已经完成。\n\n"
    "判断规则：\n"
    "- 如果回复中明确表达了即将执行某个操作的意图（如'我将'、'我会'、'请稍等'等），"
    "且没有同时确认该操作已经完成，返回 true。\n"
    "- 如果回复中已经确认动作完成（如'已经打开'、'已完成'、'搞定了'等），返回 false。\n"
    "- 如果回复与执行操作无关（纯闲聊），返回 false。\n"
    "- 如果回复既表达了意图又确认了完成（如'我将帮你打开，已经打开了'），返回 false。\n\n"
    "只返回 JSON：{\"need_retry\": true} 或 {\"need_retry\": false}，不要返回其他内容。"
)


class ValidatorAgent:
    """模型行动性表态的三级核查。

    当模型说"我将关闭床头灯"但没有说"已经关闭"时，
    自动追加提示消息让模型继续执行。
    """

    def __init__(self, max_retries: int = 1, llm: ChatOpenAI | None = None,
                 ha_service=None):
        self._max_retries = max_retries
        self._llm = llm
        # HA 服务：断言核查时读实体真实状态（get_states_snapshot，5s 缓存）。
        # 由 Dispatcher 注入（与自身持有的同一实例），HA 热替换时经
        # set_ha_service 重绑。
        self._ha_service = ha_service
        # should_retry 判定"需要重试"时留下的定向重试消息（状态不符/幻觉），
        # build_retry_message 优先取用；轮首清空防串轮。
        self._pending_retry_message: HumanMessage | None = None
        # per-user LLM 缓存（user_id → ChatOpenAI），仿 dispatcher._user_agents 模式。
        # 主聊天重试时 validator 与主对话用同一模型，避免全局/用户模型不一致误判。
        # user_id 为空（APP_TOKEN 鉴权等）走全局 self._llm。
        # 注意：key 解析是 async（resolve_key_for_role_user），在 should_retry 内完成；
        # 这里只缓存已构建的 ChatOpenAI 实例。
        self._user_llms: dict[str, ChatOpenAI] = {}
        # per-user LLM 注入的 httpx 客户端（生命周期由本类负责——ChatOpenAI
        # 不会关注入的客户端）。invalidate_user 不关闭的话连接池缓慢累积。
        self._user_clients: dict[str, tuple] = {}
        self._close_tasks: set = set()

    @property
    def max_retries(self) -> int:
        """校验重试上限（dispatcher 的失败重试上限以此对齐，避免跨类读私有属性）。"""
        return self._max_retries

    def set_ha_service(self, svc) -> None:
        """HA 配置热替换后重绑（dispatcher.set_ha_service 转发调用）。"""
        self._ha_service = svc

    def invalidate_user(self, user_id: str) -> None:
        """用户修改 chat key 后清除其缓存的 per-user LLM，下次 should_retry 重建。

        与 dispatcher.invalidate_user_agent 对齐——key 变更后旧 LLM 实例
        （带旧 api_key）必须清除，否则 validator 用旧 key 请求会误判或报错。
        user_id 为空或未缓存则 no-op。
        """
        old = self._user_llms.pop(user_id, None)
        clients = self._user_clients.pop(user_id, None)
        if clients is not None:
            self._close_clients(clients)
        if old is not None:
            logger.info("Validator: invalidated cached LLM for user_id=%s", user_id)

    def _close_clients(self, clients: tuple) -> None:
        """关闭注入的 (sync, async) httpx 客户端。失败静默（仅资源回收）。"""
        sync_c, async_c = clients
        try:
            sync_c.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            t = loop.create_task(async_c.aclose())
            self._close_tasks.add(t)
            t.add_done_callback(self._close_tasks.discard)
        except RuntimeError:
            pass  # 无运行中的循环（停机路径），交由 GC

    async def close_all(self) -> None:
        """关闭全部 per-user 客户端（进程停机时调用）。"""
        clients = list(self._user_clients.values())
        self._user_clients.clear()
        self._user_llms.clear()
        for c in clients:
            self._close_clients(c)
            try:
                await c[1].aclose()
            except Exception:  # noqa: BLE001
                pass

    def _get_llm(self, user_id: str = "") -> ChatOpenAI:
        """按 user_id 取已缓存的 per-user LLM；未缓存或无 user_id 回退全局。

        per-user LLM 的构建（含 async key 解析）由 _resolve_user_llm 完成，
        should_retry 调用它后本方法从缓存取。
        """
        if user_id and user_id in self._user_llms:
            return self._user_llms[user_id]
        # 无 per-user 缓存：走全局
        if self._llm is None:
            self._llm = self._build_llm()
        return self._llm

    async def _resolve_user_llm(self, user_id: str) -> ChatOpenAI | None:
        """按 user_id 解析 per-user chat key 并构建 LLM，缓存后返回。

        在 async 上下文（should_retry）内调用。用户无 per-user 配置返回 None，
        调用方回退全局 _get_llm(user_id)（命中全局分支）。
        """
        if not user_id or user_id in self._user_llms:
            return self._user_llms.get(user_id)
        try:
            from ..core.key_resolver import resolve_key_for_role_user
            key_info = await resolve_key_for_role_user("chat", user_id)
            if not key_info or not key_info.get("api_key"):
                return None
            from ..clients.http_client import new_client, new_sync_client
            sync_c = new_sync_client(timeout=30.0)
            async_c = new_client(timeout=30.0)
            llm = ChatOpenAI(
                model=key_info.get("model", "glm-4-flash"),
                base_url=key_info.get("base_url", "").rstrip("/"),
                api_key=key_info["api_key"],
                temperature=0.0,
                max_tokens=50,
                http_client=sync_c,
                http_async_client=async_c,
            )
            self._user_llms[user_id] = llm
            self._user_clients[user_id] = (sync_c, async_c)
            logger.info("Validator: built per-user LLM for user_id=%s, model=%s",
                        user_id, key_info.get("model"))
            return llm
        except Exception:
            logger.debug("Validator: failed to build per-user LLM, will fallback to global", exc_info=True)
            return None

    @staticmethod
    def _build_llm() -> ChatOpenAI:
        """复用 chat 角色的【全局】模型配置构建轻量 LLM 实例。"""
        from ..clients.http_client import new_client, new_sync_client
        http_client = new_sync_client(timeout=30.0)
        http_async_client = new_client(timeout=30.0)

        key_entry = resolve_key_for_role("chat")

        if key_entry:
            base_url = key_entry.get("base_url", "").rstrip("/")
            model = key_entry.get("model", "glm-4-flash")
            api_key = key_entry.get("api_key", "")
            return ChatOpenAI(
                model=model,
                base_url=base_url,
                api_key=api_key or "not-needed",
                temperature=0.0,
                max_tokens=50,
                http_client=http_client,
                http_async_client=http_async_client,
            )

        base_url = str(get_config("llm.base_url", "http://127.0.0.1:11434")).rstrip("/")
        model = str(get_config("llm.chat_model", "qwen3.5:9b"))
        if "127.0.0.1" in base_url or "localhost" in base_url:
            if not base_url.endswith("/v1"):
                base_url = base_url + "/v1"
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key="not-needed",
            temperature=0.0,
            max_tokens=50,
            http_client=http_client,
            http_async_client=http_async_client,
        )

    # ------------------------------------------------------------------
    # 断言核查流水线（代码驱动，零 LLM 调用）
    # ------------------------------------------------------------------

    async def _read_entity_state(self, entity_id: str) -> str | None:
        """读实体当前 state。HA 未注入/读取失败返回 None（不可核 → 通用重试）。"""
        svc = self._ha_service
        if svc is None:
            return None
        try:
            states = await svc.get_states_snapshot()
        except Exception:
            logger.warning("Validator: 读取实体状态失败", exc_info=True)
            return None
        for s in states or []:
            if s.get("entity_id") == entity_id:
                st = s.get("state")
                return str(st) if st is not None else None
        return None

    async def _verify_action_claims(self, final_content: str, query: str,
                                    entity_name_map: dict[str, str] | None) -> bool | None:
        """核查回复中的行动性表态。返回 None 表示无表态（调用方决定是否走 LLM 兜底）；
        True = 需要重试（可能留下定向 _pending_retry_message）；False = 静默通过。"""
        m = _DONE_CLAIM_RE.search(final_content)
        tense = "past"
        if m is None:
            m = _DOING_CLAIM_RE.search(final_content)
            tense = "doing"
        if m is None:
            m = _WILL_CLAIM_RE.search(final_content)
            tense = "will"
        if m is None:
            return None

        verb = m.group(m.lastgroup) if m.lastgroup else ""
        before, after = _claim_contexts(final_content, m)
        entity = _match_entity(after, entity_name_map) or _match_entity(before, entity_name_map)
        claim_text = m.group(0)

        if entity is None:
            # 两级兜底：断言或 query 里出现设备名词 → 说的确实是设备但对不上
            # 实体（幻觉信号），重试让模型老实查 get_entities；两者都没有 →
            # 闲聊（"方案已经设置好了"），静默通过。
            if _DEVICE_NOUN_RE.search(claim_text + after) or (query and _CONTROL_INTENT_RE.search(query)):
                self._pending_retry_message = self._build_no_entity_retry_message(claim_text + after)
                logger.info("Validator: 断言「%s」未匹配到真实设备 → 重试核查", claim_text + after)
                return True
            logger.info("Validator: 断言「%s」无设备语义（闲聊），静默通过", claim_text)
            return False

        eid, friendly = entity
        if tense == "will":
            # 将要X + 0 次工具调用 = 确定性未执行（用户下了指令而活没干），
            # 无需查状态即可判撒谎，直接重试。
            logger.info("Validator: 将要时态断言「%s」（%s）但 0 次工具调用 → 重试",
                        claim_text, eid)
            return True

        actual = await self._read_entity_state(eid)
        expected = _expected_state(verb, eid)
        if expected is None or actual is None:
            # 状态语义不可核（调温/媒体等）或读不到状态 → 退回通用强制重试
            # （与旧硬规则行为一致，宁可多跑一轮不可漏检）
            logger.info("Validator: 断言「%s」状态不可核（expected=%s actual=%s）→ 通用重试",
                        claim_text, expected, actual)
            return True
        if actual == expected:
            # 断言与真实状态一致：设备本来就处于该状态，静默通过，不再瞎重试
            logger.info("Validator: 断言「%s」与真实状态一致（%s=%s），静默通过",
                        claim_text, friendly, actual)
            return False
        self._pending_retry_message = self._build_state_mismatch_retry_message(
            claim_text, friendly, eid, actual)
        logger.info("Validator: 断言「%s」与真实状态不符（%s 实际=%s）→ 定向重试",
                    claim_text, friendly, actual)
        return True

    # ------------------------------------------------------------------
    # LLM 语义校验（第二层兜底：正则漏网的非常规句式）
    # ------------------------------------------------------------------

    async def _llm_semantic_check(self, final_content: str, user_id: str) -> bool:
        """LLM 判断回复是否"表达了意图但未确认完成"。判定逻辑与旧版一致。"""
        # per-user 优先：解析用户 chat key 构建专用 LLM（缓存），失败/无配置回退全局
        if user_id:
            await self._resolve_user_llm(user_id)
        llm = self._get_llm(user_id)
        messages = [
            SystemMessage(content=_VALIDATOR_SYSTEM_PROMPT),
            HumanMessage(content=final_content[:500]),  # 截断防止超长
        ]

        try:
            # 记录 LLM 调用
            try:
                from ..container import get_container
                get_container().metrics_service.record_llm_call()
            except Exception:  # noqa: BLE001
                pass

            response = await llm.ainvoke(messages)
            text = response.content.strip() if response.content else ""
            logger.info("Validator: content=%r..., validator_response=%r",
                        final_content[:80], text[:80])
            # 解析 JSON 响应：优先 json.loads 精确解析 need_retry 字段，
            # LLM 偶尔返回非 JSON 时降级到词边界匹配 "true"（避免 "true story" 误判）。
            return self._parse_need_retry(text)
        except Exception:
            logger.exception("Validator: LLM call failed, fallback to no retry")
            # 记录 LLM 调用错误
            try:
                from ..container import get_container
                get_container().metrics_service.record_llm_call(error=True)
            except Exception:  # noqa: BLE001
                pass
            return False

    async def should_retry(self, final_content: str, tool_call_count: int,
                           user_id: str = "", query: str = "",
                           entity_name_map: dict[str, str] | None = None) -> bool:
        """判断模型是否需要重试。

        Args:
            final_content: 模型最终输出的文本内容
            tool_call_count: 工具调用次数（断言核查只在 0 次时适用——有真实
                工具调用的失败已由 Dispatcher 失败重试回路覆盖）
            user_id: 当前用户 ID，用于解析 per-user chat key（LLM 兜底层用）。
            query: 用户本轮输入，控制意图判定用（实体指称缺位时的第二信号 +
                LLM 兜底层的闸门）。空串 = 调用方未传（测试），按无意图处理。
            entity_name_map: {entity_id: friendly_name}，断言实体匹配用。

        Returns:
            True 表示需要重试
        """
        self._pending_retry_message = None
        if not final_content.strip():
            logger.debug("Validator: empty content, skip retry")
            return False

        if bool(get_config("chat_assistant.claim_verify_enabled", True)):
            # ── 新流水线：代码驱动的断言核查 ──
            if tool_call_count == 0:
                verdict = await self._verify_action_claims(final_content, query, entity_name_map)
                if verdict is not None:
                    return verdict
                # 无行动性表态：query 带控制意图才花一次 LLM 语义校验兜底；
                # 纯闲聊 query 零调用（旧版这里每轮必付一次 LLM 调用）。
                if query and _CONTROL_INTENT_RE.search(query):
                    return await self._llm_semantic_check(final_content, user_id)
                logger.debug("Validator: 无行动性表态且 query 无控制意图，跳过: query=%r",
                             (query or "")[:40])
                return False
            # tool_call_count > 0（调度器正常不会传入）：断言核查不适用，
            # query 有意图时保留 LLM 语义兜底。
            if query and _CONTROL_INTENT_RE.search(query):
                return await self._llm_semantic_check(final_content, user_id)
            return False

        # ── 旧行为回退（chat_assistant.claim_verify_enabled=False）──
        # 硬性规则：声称已完成设备控制操作但没调任何工具 → 撒谎，强制重试，
        # 不浪费一次 LLM 语义判断调用。_ACTION_DONE_RE 只匹配"已+控制动词"
        # 强完成态，闲聊不会误触发。
        if tool_call_count == 0 and _ACTION_DONE_RE.search(final_content):
            logger.info("Validator: 检测到声称已完成控制操作但 tool_calls=0，强制重试: %r",
                        final_content[:80])
            return True
        return await self._llm_semantic_check(final_content, user_id)

    @staticmethod
    def _parse_need_retry(text: str) -> bool:
        """解析 validator LLM 的返回，判断是否 need_retry。

        优先 json.loads 精确解析 {"need_retry": true/false}；
        LLM 偶尔返回纯 "true"/"false" 时按词边界判定。
        其它非 JSON 解释性文本（如 "true story"）一律不重试——
        原代码 "true" in text 会把这类子串误判为需重试。
        """
        text = text.strip()
        # 尝试精确 JSON 解析
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return bool(parsed.get("need_retry", False))
            if isinstance(parsed, bool):
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        # 降级：只有整段文本就是独立的 true（去掉空格后）才算，
        # 避免把 "true story" / "not true" 这类解释性文本误判为需重试。
        return text.lower() in ("true", "yes", "1")

    # ------------------------------------------------------------------
    # 重试消息
    # ------------------------------------------------------------------

    @staticmethod
    def _build_state_mismatch_retry_message(claim_text: str, friendly: str,
                                            entity_id: str, actual: str) -> HumanMessage:
        """状态不符的定向重试消息：注入真实状态，比通用"你必须调工具"服从率高。"""
        state_text = {"on": "开启(on)", "off": "关闭(off)",
                      "open": "打开(open)", "closed": "合上(closed)"}.get(actual, actual)
        return HumanMessage(
            content=(
                f"你刚才回复「{claim_text}」，但系统核实 {friendly}（{entity_id}）"
                f"当前状态为「{state_text}」，与你的说法不符。\n"
                "请立即通过 tool_call 真实执行：先 get_entities 确认该设备实体与可控项，"
                "再用 call_service 执行操作。\n"
                "以工具返回的真实结果为准，禁止在未调用工具的情况下声称已完成。"
            )
        )

    @staticmethod
    def _build_no_entity_retry_message(claim_text: str) -> HumanMessage:
        """断言对不上真实设备的重试消息（幻觉/指称模糊共用）。"""
        return HumanMessage(
            content=(
                f"你刚才回复「{claim_text}」，但系统在设备列表中找不到对应的真实设备。\n"
                "请立即调用 get_entities 查看真实设备列表：若设备存在，"
                "用 call_service 真实执行；若不存在，如实告知用户。\n"
                "禁止编造 entity_id，禁止在未调用工具的情况下声称已完成。"
            )
        )

    @staticmethod
    def has_rule_create_claim(final_content: str) -> bool:
        """回复是否泄漏了规则创建行为（确定性正则，门控配套核查用）。

        两种形态都算：a) 完成态声称"已创建规则/创建成功"；b) 把创建工具调用当
        正文输出（回复里出现工具名 automation_rule_create——无权限回合它不该
        知道这个工具）。教学句「可以说『创建规则』」不会命中。
        """
        text = final_content or ""
        return bool(_RULE_CREATE_CLAIM_RE.search(text) or _RULE_TOOL_LEAK_RE.search(text))

    @staticmethod
    def build_rule_create_claim_retry_message(claim_text: str) -> HumanMessage:
        """声称已创建规则 / 泄漏创建工具文本的定向重写消息。"""
        return HumanMessage(
            content=(
                f"你刚才回复「{claim_text}」，但本轮没有调用任何规则创建工具，"
                "没有任何规则被创建——这个说法不属实，必须纠正。\n"
                "请重新回复用户，按用户真实意图二选一：\n"
                "1) 用户想立即执行设备操作：直接调用设备控制工具真实执行"
                "（先 get_entities 确认实体，再 call_service），以工具返回为准；\n"
                "2) 用户确实想创建自动化规则：如实告知用户，请其明确说"
                "「创建规则：…」才会进入创建流程。\n"
                "绝对不要在未调用规则创建工具的情况下声称已创建规则；"
                "也不要在回复里模仿工具调用的格式或输出任何工具参数 JSON 文本。"
            )
        )

    def build_retry_message(self) -> HumanMessage:
        """构建重试提示消息。有定向消息（状态不符/幻觉）时优先用定向的。"""
        pending = self._pending_retry_message
        self._pending_retry_message = None  # 取走即清，防串轮
        if pending is not None:
            return pending
        return HumanMessage(
            content="你刚才只输出了文字回复，没有通过 tool_call 调用任何工具。"
                    "你必须立即通过 tool_call 机制调用必要的工具来执行操作。"
                    "绝对不要在回复文本中写 JSON 代码块来模拟工具调用。"
                    "现在请立即调用工具。"
        )
