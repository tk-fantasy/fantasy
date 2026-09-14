"""待确认草稿（两段式确认）的共享存取逻辑。

automation_rule_create 只解析不落库，把草稿挂在 SessionState.pending_confirmations
上（10 分钟 TTL），确认动作有两条入口：

- 工具路径（语音/飞书等）：用户口头说「确认」→ automation_rule_confirm
- REST 路径（网页）：确认弹窗直接调 /api/rules/pending/{id}/confirm

两条路径共用本模块，落库口径（实体存在性校验、pop 草稿）保持一致；渠道差异
只体现在各自的返回值格式上（工具要 hint，REST 要 message），故本模块只返回
中立的结果字典，不构造任何一端特有的错误结构。
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# 待确认草稿的有效期（秒）。确认动作与创建动作通常紧邻，超时即过期防陈旧落库。
PENDING_TTL_SECONDS = 600

KIND_AUTOMATION_RULE = "automation_rule"


def pending_store(session: Any) -> dict:
    """取会话的两段式确认暂存区（get-or-init，兼容旧反序列化会话对象）。

    pending_confirmations 刻意不持久化：草稿短命（10 分钟 TTL），重启丢了让
    用户重说，不值得为它扩展序列化 schema。因此反序列化回来的会话对象可能
    没有这个字段，这里负责补上；只读桩上 setattr 失败则退化为本次内有效。
    """
    store = getattr(session, "pending_confirmations", None)
    if not isinstance(store, dict):
        store = {}
        try:
            setattr(session, "pending_confirmations", store)
        except Exception:  # noqa: BLE001
            pass
    return store


def get_live_pending(session: Any, pending_id: str, kind: str) -> dict | None:
    """取未过期的指定类型草稿；过期即删并返回 None（懒过期）。"""
    entry = pending_store(session).get(pending_id)
    if entry is None or entry.get("kind") != kind:
        return None
    if time.time() - float(entry.get("created_at", 0.0)) > PENDING_TTL_SECONDS:
        pending_store(session).pop(pending_id, None)
        return None
    return entry


def resolve_pending(session: Any, kind: str) -> tuple[str | None, dict | None, int]:
    """会话内该 kind 的未过期草稿恰好 1 个时返回 (id, entry, 1)，否则 (None, None, 数量)。

    顺带对遍历到的过期草稿做懒清理。数量返回给调用方用于区分「没有草稿」和
    「有多个草稿、需要用户指明」两种失败——提示语完全不同。
    """
    store = pending_store(session)
    now = time.time()
    found: tuple[str, dict] | None = None
    count = 0
    for pid, entry in list(store.items()):
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            continue
        if now - float(entry.get("created_at", 0.0)) > PENDING_TTL_SECONDS:
            store.pop(pid, None)
            continue
        count += 1
        found = (pid, entry)
    if count == 1 and found is not None:
        return found[0], found[1], 1
    return None, None, count


def locate_pending(session: Any, pending_id: str, kind: str) -> tuple[str | None, dict | None, str]:
    """定位草稿，返回 (pending_id, entry, error)；error 非空即失败。

    pending_id 命中直接用；缺失或已失效时回退到「会话内唯一未过期草稿」。
    这个兜底是口头确认路径能用的前提：session.model_messages 不持久化 tool
    消息（工具结果只内联进 assistant 回复），跨轮后模型看不到上一轮工具返回的
    pending_id，用户第二轮说「确认」时它无 id 可传。
    """
    if pending_id:
        entry = get_live_pending(session, pending_id, kind)
        if entry is not None:
            return pending_id, entry, ""
    fallback_id, fallback_entry, live_count = resolve_pending(session, kind)
    if fallback_entry is not None and fallback_id is not None:
        return fallback_id, fallback_entry, ""
    if live_count > 1:
        return None, None, f"有 {live_count} 个待确认规则，请让用户指明要处理哪一个"
    if pending_id:
        return None, None, "待确认规则不存在或已过期"
    return None, None, "当前没有待确认的规则"


async def find_missing_entities(rule: dict, ha_service: Any, ha_client_ref: list) -> list[str]:
    """落库前轻量校验：actions 里引用的 entity_id 是否仍存在于 HA。

    校验本身失败（HA 不可达/测试桩无此能力）放行——与 call_service 的
    「校验失败放行」口径一致，不因检查不可用而锁死创建。
    """
    entity_ids: list[str] = []
    for action in rule.get("actions") or []:
        tool_input = action.get("mcp_tool_input") or {}
        eid = str(tool_input.get("entity_id", "") or "").strip()
        if eid:
            entity_ids.append(eid)
    if not entity_ids:
        return []
    try:
        # 延迟导入：tools.py 在模块级导入本模块，反向只能函数内导入避免环。
        from ..tools import _states_for_existence_check
        states = await _states_for_existence_check(ha_service, ha_client_ref[0])
    except Exception:  # noqa: BLE001
        return []
    real = {s.get("entity_id") for s in states}
    return [e for e in entity_ids if e not in real]


def is_vision_rule(rule: dict) -> bool:
    """这条规则是否靠摄像头画面判断。

    type 缺失/非法一律按 vision —— 与 rule_service.build_rule 的兜底方向一致
    （兜到 vision 至少不会让规则静默失效），也与前端 utils/ruleMismatch.js 同口径。
    """
    return str(rule.get("type", "") or "").strip().lower() not in ("time", "weather")


def needs_camera(rule: dict) -> bool:
    """这条规则是否还缺一路摄像头绑定。

    判据与前端 utils/ruleMismatch.js 的 red 分支一致（type=vision 且 camera_id 空）：
    automation_service 对 camera_id 为空的规则在**所有**摄像头上都评估，所以一条
    未绑定的视觉规则等于"任意一路画面有人都触发"，是项目自己认定的危险态。
    """
    return is_vision_rule(rule) and not str(rule.get("camera_id", "") or "").strip()


# 创建规则的门控词：消息里同时出现「规则」和创建类动词才算明确的创建意图。
# 必须带「规则」二字——「创建场景」「创建定时任务」是别的功能，不能被这里抢走。
_CREATE_VERBS = (
    "创建", "新建", "建立", "添加", "增加", "设置", "定义",
    "做个", "建个", "加个", "定个", "写个", "弄个", "来个", "设个",
    "做条", "建条", "加条", "定条", "写条", "弄条", "来条", "设条",
)


# 规则查询话术：消息里出现「规则」+ 查询类动词/疑问词。查询轮需要
# automation_rule_list 可见（clean 变体整族剔除会让模型如实回答"没有这工具"）。
_RULE_QUERY_WORDS = ("查", "列", "看", "哪些", "什么", "多少", "列表", "介绍", "说明")


def wants_rule_query(text: str) -> bool:
    """用户本轮是否在查询（而非创建）自动化规则。

    与 wants_rule_creation 互补：创建意图优先级更高（同为 full 变体，语义上
    分开写保持各自可调）。只认「规则」二字开头限定，避免"查看一下"这类无
    「规则」的闲聊误放行。
    """
    t = str(text or "")
    if "规则" not in t:
        return False
    if wants_rule_creation(t):
        return False
    return any(w in t for w in _RULE_QUERY_WORDS)


def wants_rule_creation(text: str) -> bool:
    """用户本轮消息是否明确要求创建规则（关键词门控，确定性判断）。

    glm-4-flash 对「如果…就…」条件式话术经常直接调 call_service 执行设备
    动作，甚至零工具调用幻觉"已创建"——提示词引导不住没调工具的轮次。所以
    「要不要创建」收口成关键词判断：
    - 命中：提示词层强制模型考虑创建（prompt_service 注入本轮指令）；
    - 未命中：automation_rule_create 工具层直接拒绝（tools.py 硬门）。
    两层共用本函数，口径单一。
    """
    t = str(text or "")
    if "规则" not in t:
        return False
    return any(v in t for v in _CREATE_VERBS)


async def confirm_pending(
    session: Any,
    pending_id: str,
    registry: Any,
    ha_service: Any,
    ha_client_ref: list,
    user_id: str = "",
    known_cameras: list | None = None,
) -> dict:
    """草稿 → 摄像头绑定校验 → 实体校验 → 落库 → 摘除草稿。

    Args:
        known_cameras: 可选的 [{"id","name"}] 列表，仅用于在拒绝时给出候选名字。
            不传也会执行校验——「视觉规则必须绑定摄像头」这条不变量与是否知道
            有哪些摄像头无关。

    校验放在这里而不是各调用方：网页走 REST 路由、语音走 automation_rule_confirm
    工具，两条路都汇到本函数。只在路由里校验的话，工具路径能绕过去落库一条
    未绑定的全局视觉规则（automation_service 对它在**每一路**摄像头上都评估）。

    Returns:
        成功：{"ok": True, "rule_id", "name", "summary", "pending_id"}
        失败：{"ok": False, "reason", "error"}；reason ∈
        not_found / camera_required / missing_entities / save_failed，供调用方映射
        各自的错误形态（工具转 tool_error + hint，REST 转对应 HTTP 状态码）。
    """
    resolved_id, entry, err = locate_pending(session, pending_id, KIND_AUTOMATION_RULE)
    if entry is None or resolved_id is None:
        return {"ok": False, "reason": "not_found", "error": err}
    rule = entry["rule"]
    # camera_chosen 由 set_pending_camera 打上：用户**显式**做过一次选择。
    # 光看 rule 区分不出「显式选了全部摄像头（camera_id="")」和「压根没选」，
    # 两者都是空串——而前者合法、后者是要拦的危险态。
    if needs_camera(rule) and not entry.get("camera_chosen"):
        names = [str(c.get("name") or c.get("id") or "")
                 for c in (known_cameras or []) if isinstance(c, dict)]
        names = [n for n in names if n]
        suffix = f"；可选摄像头：{'、'.join(names)}" if names else ""
        return {
            "ok": False,
            "reason": "camera_required",
            "error": "这条规则靠摄像头画面判断，但还没绑定看哪一路，不能创建"
                     "（不绑定的话任意一路画面有人都会触发）" + suffix,
            "candidates": names,
        }
    missing = await find_missing_entities(rule, ha_service, ha_client_ref)
    if missing:
        return {
            "ok": False,
            "reason": "missing_entities",
            "error": f"规则动作引用的设备已不存在: {', '.join(missing)}",
            "missing_entities": missing,
        }
    try:
        saved = registry.add_rule(rule, user_id=user_id)
    except Exception as exc:
        logger.warning("confirm_pending 落库失败: %s", exc, exc_info=True)
        return {"ok": False, "reason": "save_failed", "error": f"规则保存失败：{exc}"}
    pending_store(session).pop(resolved_id, None)
    return {
        "ok": True,
        "pending_id": resolved_id,
        "rule_id": saved.get("id"),
        "name": saved.get("name", ""),
        "summary": str(saved.get("summary", "")),
    }


def cancel_pending(session: Any, pending_id: str) -> bool:
    """摘除草稿（不落库）。返回是否真的删掉了东西。"""
    resolved_id, entry, _ = locate_pending(session, pending_id, KIND_AUTOMATION_RULE)
    if entry is None or resolved_id is None:
        return False
    pending_store(session).pop(resolved_id, None)
    return True


def set_pending_camera(session: Any, pending_id: str, camera_id: str) -> dict:
    """把草稿绑定到某路摄像头（camera_id="" 表示显式选择"全部摄像头/全局"）。

    本模块是纯状态层，不 import camera_manager —— camera_id 是否真实存在由调用方
    （REST 路由 / 飞书插件）拿 camera_manager.list_cameras() 校验后再传进来。

    Returns:
        成功：{"ok": True, "pending_id", "rule"}
        失败：{"ok": False, "reason": "not_found", "error": ...}
    """
    resolved_id, entry, err = locate_pending(session, pending_id, KIND_AUTOMATION_RULE)
    if entry is None or resolved_id is None:
        return {"ok": False, "reason": "not_found", "error": err}
    rule = entry.get("rule")
    if not isinstance(rule, dict):
        return {"ok": False, "reason": "malformed", "error": "草稿内容已损坏，请重新描述需求"}
    rule["camera_id"] = str(camera_id or "").strip()
    entry["rule"] = rule
    # 记下"用户显式选过"：camera_id="" 既可能是显式选全局（合法），也可能是
    # 从没选过（要拦）。confirm_pending 靠这个标记区分两者。
    entry["camera_chosen"] = True
    entry["created_at"] = time.time()  # 改完重新计时，与 revise 口径一致
    return {"ok": True, "pending_id": resolved_id, "rule": rule}
