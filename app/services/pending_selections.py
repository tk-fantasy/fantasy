"""待选设备草稿（消歧两段式确认）的共享存取逻辑。

`call_service` 闸门判定目标设备有歧义（ambiguous / category_miss）时不执行、
也不让模型猜，而是把 LLM 已解析好的动作（domain/service/data）连同候选挂到
`SessionState.pending_confirmations` 上。确认动作有两条入口：

- 网页路径：消歧弹框勾选 → `POST /api/ha/pending/{id}/select`
- 工具路径（语音/飞书）：模型口头列举候选 → 用户下一轮直接说设备名 →
  闸门落到 exact/unique 正常执行，同时由 `drop_selection_drafts` 清掉这份草稿

与 `pending_rules` 同构，同样复用 `pending_store` / `locate_pending` /
`PENDING_TTL_SECONDS`（暂存区按 kind 泛化，无需扩展 SessionState schema）。
渠道差异只体现在各自的返回值格式上（工具要 hint，REST 要 message），故本模块
只返回中立的结果字典，不构造任何一端特有的错误结构，也不碰 HA —— 执行由调用方
注入的 executor 完成。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from ..core.exceptions import AppException
from .pending_rules import locate_pending, pending_store

logger = logging.getLogger(__name__)

KIND_DEVICE_SELECTION = "device_selection"

# 弹框文案按 reason 分支：ambiguous 是「你要操作哪个」，
# category_miss 必须先如实说「没找到 X」再给候选，不能假装 X 存在
REASON_AMBIGUOUS = "ambiguous"
REASON_CATEGORY_MISS = "category_miss"

# locate_pending 的错误文案写的是「待确认规则」（pending_rules 的口径），
# 直接透出去会让用户以为在说自动化规则，故本模块自己给文案
_NOT_FOUND = "待选设备不存在或已过期，请重新说一遍指令"

# executor(draft, entity_id) -> 执行结果。由调用方（REST 路由）注入，
# entity_id 是逗号拼接的批量形态，与 HA / call_service 的批量口径一致
Executor = Callable[[dict, str], Awaitable[Any]]


def create_selection_draft(
    session: Any,
    *,
    query: str,
    domain: str,
    service: str,
    data: dict | None,
    candidates: list[dict],
    reason: str = REASON_AMBIGUOUS,
) -> str:
    """挂一份待选草稿，返回 pending_id。

    candidates 元素口径 = `device_registry.build_match_index` 的条目
    （至少含 entity_id / label / domain / area_name / state），弹框直接渲染。
    """
    pending_id = f"sel-{uuid4().hex[:12]}"
    pending_store(session)[pending_id] = {
        "kind": KIND_DEVICE_SELECTION,
        "created_at": time.time(),
        "query": query or "",
        "domain": domain or "",
        "service": service or "",
        "data": data or {},
        "candidates": list(candidates or []),
        "reason": reason,
    }
    return pending_id


def _candidate_index(entry: dict) -> dict[str, dict]:
    return {
        str(c.get("entity_id", "")): c
        for c in (entry.get("candidates") or [])
        if isinstance(c, dict) and c.get("entity_id")
    }


async def confirm_selection(
    session: Any,
    pending_id: str,
    selected_ids: list[str],
    executor: Executor,
) -> dict:
    """校验选择 → 执行 → 摘除草稿。

    Returns:
        成功：{"ok": True, "pending_id", "entity_ids", "names", "result"}
        失败：{"ok": False, "reason", "error"}；reason ∈
        not_found / invalid_selection / exec_failed，供调用方映射各自的错误形态。
        失败一律**不摘草稿**：用户可以在 TTL 内重选，不必重说一遍指令。

    Raises:
        AppException: executor 抛出的路由层业务异常原样上抛（保住 403 这类状态码，
            不被压成 exec_failed/502）；草稿同样保留。
    """
    resolved_id, entry, _err = locate_pending(session, pending_id or "", KIND_DEVICE_SELECTION)
    if entry is None or resolved_id is None:
        return {"ok": False, "reason": "not_found", "error": _NOT_FOUND}
    by_id = _candidate_index(entry)
    picked = [str(e).strip() for e in (selected_ids or []) if str(e).strip()]
    if not picked:
        return {"ok": False, "reason": "invalid_selection", "error": "未选择任何设备"}
    # 只允许从候选里挑：弹框若能提交任意 entity_id，就等于开了一条绕过闸门
    # （以及绕过 entity_operable 黑名单）的后门
    invalid = [e for e in picked if e not in by_id]
    if invalid:
        return {
            "ok": False,
            "reason": "invalid_selection",
            "error": f"所选设备不在候选内: {', '.join(invalid)}",
        }
    try:
        result = await executor(entry, ",".join(picked))
    except AppException:
        # 路由层业务异常（如设备已被禁止 AI 操作 → 403）原样上抛，不能被压成
        # exec_failed/502 丢掉状态码；草稿同样不摘，解除限制后用户可重试
        raise
    except Exception as exc:
        logger.warning("confirm_selection 执行失败: %s", exc, exc_info=True)
        return {"ok": False, "reason": "exec_failed", "error": f"执行失败：{exc}"}
    pending_store(session).pop(resolved_id, None)
    return {
        "ok": True,
        "pending_id": resolved_id,
        "entity_ids": picked,
        "names": [str(by_id[e].get("label") or by_id[e].get("name") or e) for e in picked],
        "result": result,
    }


def cancel_selection(session: Any, pending_id: str) -> bool:
    """摘除草稿（不执行）。返回是否真的删掉了东西。"""
    resolved_id, entry, _err = locate_pending(session, pending_id or "", KIND_DEVICE_SELECTION)
    if entry is None or resolved_id is None:
        return False
    pending_store(session).pop(resolved_id, None)
    return True


def drop_selection_drafts(session: Any, *, except_query: str | None = None) -> int:
    """清掉会话内待选草稿，返回清掉的数量（不动其他 kind 的草稿）。

    闸门干净解决一轮指令（exact / unique / all_marker）时调用。语音渠道用户被问
    「要开哪个」之后直接说「客厅吊灯」，走的就是这条路径——草稿留着只会在 TTL 内
    被 `locate_pending` 的「会话内唯一草稿」兜底误命中，把新指令的结果确认掉。

    Args:
        except_query: 保留该 query 的草稿。一轮里模型可能连调多次工具
            （「开灯关窗帘」），第一次刚为「开灯」建的草稿不能被第二次的
            清场动作抹掉，否则用户点弹框时草稿已不在（只能报过期）。
    """
    store = pending_store(session)
    dropped = 0
    for pid, entry in list(store.items()):
        if not isinstance(entry, dict) or entry.get("kind") != KIND_DEVICE_SELECTION:
            continue
        if except_query is not None and entry.get("query") == except_query:
            continue
        store.pop(pid, None)
        dropped += 1
    return dropped
