"""规则路由 — 自动化规则的 CRUD + 待确认草稿（网页确认弹窗）。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user, require_owned_session
from ..core.exceptions import AppException
from ..schema.api_schemas import (
    RuleCreateRequest,
    RulePayloadRequest,
    RuleEnabledRequest,
    RuleReviseRequest,
    RuleUpdateRequest,
    ExplainRequest,
    PendingExplainRequest,
    PendingReviseRequest,
    PendingConfirmRequest,
    PendingCameraRequest,
)
from ..services.pending_rules import (
    KIND_AUTOMATION_RULE,
    cancel_pending,
    confirm_pending,
    is_vision_rule,
    locate_pending,
    needs_camera,
    set_pending_camera,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/task/rule")
async def build_rule(
    payload: RuleCreateRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    user_id = current_user.get("user_id", "")
    text = payload.text
    rule = await container.rule_service.build_rule(text, user_id=user_id, camera_id=payload.camera_id)
    condition = str(rule.get("condition", "")).strip()
    if not condition:
        raise AppException("无法从输入中解析出有效的视觉条件",
                           code="rule_parse_failed", http_status=400)
    # 零匹配拦截：自动修复救不回来的设备引用不出库（与聊天 create_handler 同口径），
    # 否则落库后两头堵——规则周期评估引用不存在的实体，永远静默失败
    if rule.pop("validation_errors", None):
        raise AppException("规则动作引用的设备不存在，且找不到可自动替换的近似设备，请换个说法或指定真实设备",
                           code="rule_device_unmatched", http_status=400)
    stored = container.rule_registry_service.add_rule(rule, user_id=user_id)
    return ApiResponse(data=stored)


@router.get("/rules")
async def list_rules(container: AppContainer = Depends(get_container)) -> ApiResponse[list[dict]]:
    return ApiResponse(data=container.rule_registry_service.list_rules())


@router.post("/rules")
async def create_rule(
    payload: RulePayloadRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    condition = payload.condition.strip()
    if not condition:
        raise AppException("规则必须包含 condition 字段",
                           code="rule_invalid", http_status=400)
    # None 的字段整个丢掉，别用 None 覆盖 add_rule 自己的默认值（旧调用方不传
    # name/summary/camera_id，行为要与扩字段之前完全一致）
    rule_dict = {k: v for k, v in payload.model_dump().items() if v is not None}
    rule_dict.setdefault("enabled", True)
    rule_dict.setdefault("cooldown_seconds", 10)
    # 与 pending confirm 同一条不变量：视觉规则必须显式绑定一路，或显式传
    # camera_id="" 表示「全部摄像头」。否则产出的就是 ruleMismatch 标红的危险规则
    # （automation_service 对未绑定的规则在每一路上都评估）。
    # 注意三态要靠 payload.camera_id is None 判断——过滤后的 rule_dict 里
    # "没传"和"传了空串（显式全局）"都表现为缺失/空，区分不出来。
    if payload.camera_id is not None:
        rule_dict["camera_id"] = _check_camera_choice(
            container, rule_dict, str(payload.camera_id).strip())
    elif needs_camera(rule_dict):
        raise AppException(
            '视觉规则必须指定摄像头（或显式传 camera_id="" 选择全部摄像头）',
            code="camera_required", http_status=400)
    return ApiResponse(data=container.rule_registry_service.add_rule(rule_dict, user_id=current_user.get("user_id", "")))


@router.post("/rules/{rule_id}/enabled")
async def set_rule_enabled(
    rule_id: str,
    payload: RuleEnabledRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    return ApiResponse(data=container.rule_registry_service.set_enabled(rule_id, payload.enabled))


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    return ApiResponse(data=container.rule_registry_service.delete_rule(rule_id))


@router.post("/rules/{rule_id}/revise")
async def revise_rule(
    rule_id: str,
    payload: RuleReviseRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """对话式修改已有规则（不落库）。

    前端把当前规则 JSON + 修改指令发来，后端用 LLM 输出新 JSON 预览。
    支持多轮：每轮的 current 是上一轮的输出，保证上下文连续。
    """
    # 优先用请求体里的 current（前端维护的多轮状态），兜底查 DB
    current = payload.current or {}
    if not current:
        stored = container.rule_registry_service.get_rule(rule_id)
        if stored is None:
            raise AppException(f"规则不存在: {rule_id}", code="rule_not_found", http_status=404)
        current = stored

    try:
        result = await container.rule_service.revise_rule(
            current, payload.instruction, user_id=current_user.get("user_id", "")
        )
    except Exception as e:
        logger.warning("revise_rule failed: %s", e, exc_info=True)
        raise AppException(f"修改失败：{e}", code="rule_revise_failed", http_status=502)
    return ApiResponse(data=result)


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    payload: RuleUpdateRequest,
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """把 revise 后确认的规则 JSON 落库。"""
    try:
        stored = container.rule_registry_service.update_rule(rule_id, payload.rule)
    except AppException:
        raise
    except Exception as e:
        logger.warning("update_rule failed: %s", e, exc_info=True)
        raise AppException(f"保存失败：{e}", code="rule_save_failed", http_status=500)
    return ApiResponse(data=stored)


@router.post("/rules/{rule_id}/explain")
async def explain_rule(
    rule_id: str,
    payload: ExplainRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """plan 模式：用自然语言回答关于当前规则的提问（只读，不修改）。

    前端传当前规则 JSON + 问题，后端用 LLM 生成解释。
    """
    current = payload.current or {}
    if not current:
        stored = container.rule_registry_service.get_rule(rule_id)
        if stored is None:
            raise AppException(f"规则不存在: {rule_id}", code="rule_not_found", http_status=404)
        current = stored
    try:
        answer = await container.rule_service.explain_rule(
            current, payload.question, user_id=current_user.get("user_id", "")
        )
    except Exception as e:
        logger.warning("explain_rule failed: %s", e, exc_info=True)
        raise AppException(f"解释失败：{e}", code="rule_explain_failed", http_status=502)
    return ApiResponse(data={"answer": answer})


# ---------------------------------------------------------------------------
# 待确认草稿 — 网页确认弹窗直接落库，绕过模型
#
# 与聊天工具 automation_rule_* 共用 services.pending_rules：草稿挂在
# SessionState.pending_confirmations（内存、10 分钟 TTL、不持久化），路径多一段
# pending 以区别于已落库规则的 /rules/{rule_id}/xxx（4 段 vs 3 段，不会互相吞掉）。
# 渠道解耦：这里只提供通用 REST 能力，不含任何渠道分支；未来飞书要做交互卡片
# 确认，全部实现在 integrations/feishu/ 插件内，调这几个端点即可。
#
# 顺序约束：前端必须等本轮 Dialog.Finish 到达后再弹确认框。dispatcher 在轮末才把
# user/assistant 消息 append 进 model_messages（agents/dispatcher.py），确认若在轮中
# 落进来，「我已确认」会排在原始请求之前，下一轮模型读到的是乱序历史。
# ---------------------------------------------------------------------------

_PENDING_GONE = "待确认规则不存在或已过期，请重新描述需求"


async def _load_pending_draft(
    container: AppContainer, session_id: str, pending_id: str, current_user: dict
):
    """校验会话归属并取出未过期草稿，返回 (session, 实际 pending_id, entry)。

    走 locate_pending 而非直接查表：与工具路径同一套兜底（传入的 id 已失效但会话内
    只剩一个草稿时仍能定位），两条确认入口的行为保持一致。
    """
    session = await require_owned_session(container, session_id, current_user)
    resolved_id, entry, _err = locate_pending(session, pending_id, KIND_AUTOMATION_RULE)
    if entry is None or resolved_id is None:
        raise AppException(_PENDING_GONE, code="pending_rule_not_found", http_status=404)
    return session, resolved_id, entry


def _known_cameras(container: AppContainer) -> list[dict] | None:
    """可用摄像头列表；camera_manager 未装配时返回 None（表示"无从判断"）。

    None 与 [] 语义不同：None = 拿不到信息（测试桩/装配未完成）→ 校验放行；
    [] = 确实一路都没有 → 视觉规则不允许落库（建一条永不触发的规则比不建更糟，
    automation_service 是按摄像头逐路评估的，没有摄像头就没有评估）。
    """
    manager = getattr(container, "camera_manager", None)
    if manager is None:
        return None
    try:
        return [c for c in (manager.list_cameras() or []) if isinstance(c, dict)]
    except Exception:  # noqa: BLE001 — 含 MagicMock 等不可迭代返回值
        logger.warning("list_cameras 失败，跳过摄像头校验", exc_info=True)
        return None


def _check_camera_choice(container: AppContainer, rule: dict, camera_id: str) -> str:
    """校验用户选的摄像头，返回规范化后的 camera_id（"" = 显式全局）。

    两层拦截：
    1. 选了具体一路但不是真实摄像头 → 400，并把可选项列进 message（模型/前端
       拿到就能自纠，与 tools.tool_error 的 candidates 口径一致）
    2. 视觉规则但一路摄像头都没有 → 400，无论选全局还是选具体哪路
    """
    cameras = _known_cameras(container)
    if cameras is None:
        return camera_id
    known = {str(c.get("id", "")) for c in cameras}
    if camera_id and camera_id not in known:
        names = "、".join(str(c.get("name") or c.get("id") or "") for c in cameras)
        raise AppException(f"摄像头不存在: {camera_id}；可选：{names or '（无）'}",
                           code="camera_not_found", http_status=400)
    if is_vision_rule(rule) and not known:
        raise AppException("没有可用摄像头，视觉规则无法触发；请先在摄像头设置里添加一路",
                           code="no_camera_available", http_status=400)
    return camera_id


@router.post("/rules/pending/{pending_id}/explain")
async def explain_pending_rule(
    pending_id: str,
    payload: PendingExplainRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """就待确认草稿提问（只读）。规则从草稿取，前端不传 current。"""
    _session, _draft_id, entry = await _load_pending_draft(
        container, payload.session_id, pending_id, current_user)
    try:
        answer = await container.rule_service.explain_rule(
            entry["rule"], payload.question, user_id=current_user.get("user_id", "")
        )
    except Exception as e:
        logger.warning("explain_pending_rule failed: %s", e, exc_info=True)
        raise AppException(f"解释失败：{e}", code="pending_explain_failed", http_status=502)
    return ApiResponse(data={"answer": answer})


@router.post("/rules/pending/{pending_id}/revise")
async def revise_pending_rule(
    pending_id: str,
    payload: PendingReviseRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """对话式修改待确认草稿（仍不落库）。改动写回草稿并重置 TTL 计时。"""
    _session, _draft_id, entry = await _load_pending_draft(
        container, payload.session_id, pending_id, current_user)
    try:
        result = await container.rule_service.revise_rule(
            entry["rule"], payload.instruction, user_id=current_user.get("user_id", "")
        )
    except Exception as e:
        logger.warning("revise_pending_rule failed: %s", e, exc_info=True)
        raise AppException(f"修改失败：{e}", code="pending_revise_failed", http_status=502)
    # entry 是暂存区里的同一个 dict，就地改即生效；草稿不持久化，无需 store_session
    entry["rule"] = result.get("rule") or entry["rule"]
    entry["created_at"] = time.time()  # 改完重新计时，给用户完整评估窗口
    return ApiResponse(data={"rule": entry["rule"], "summary": str(result.get("summary", ""))})


@router.post("/rules/pending/{pending_id}/confirm")
async def confirm_pending_rule(
    pending_id: str,
    payload: PendingConfirmRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """确认草稿 → 摄像头绑定校验 → 实体校验 → 写入 rule_registry 开始生效。"""
    session, draft_id, entry = await _load_pending_draft(
        container, payload.session_id, pending_id, current_user)
    rule = entry.get("rule") if isinstance(entry.get("rule"), dict) else {}
    if payload.camera_id is not None:
        # 显式选择："" = 「全部摄像头（全局）」，非空 = 绑定某一路。
        # 两种都算用户已经做过决定，不再用 needs_camera 二次拦截（它对
        # "显式全局"和"根本没选"给出同一个 True，区分只能靠 None/"" 三态）。
        chosen = _check_camera_choice(container, rule, str(payload.camera_id).strip())
        bound = set_pending_camera(session, draft_id, chosen)
        if not bound.get("ok"):
            raise AppException(str(bound.get("error", _PENDING_GONE)),
                               code="pending_rule_not_found", http_status=404)
    elif needs_camera(rule):
        # 没选，而这条规则又必须绑一路 → 挡住。前端禁用按钮只是体验，
        # 真正的不变量守在这里（否则直接产出 ruleMismatch 标红的危险全局规则）
        raise AppException("视觉规则必须指定摄像头（或显式选择「全部摄像头」）",
                           code="camera_required", http_status=400)
    result = await confirm_pending(
        session,
        draft_id,
        container.rule_registry_service,
        container.ha_service,
        container.ha_client_ref,
        user_id=current_user.get("user_id", ""),
    )
    if not result.get("ok"):
        # 设备已消失是用户可修正的输入问题（400），落库失败是服务端问题（500），
        # 草稿没了才是 404
        status = {"missing_entities": 400, "save_failed": 500}.get(
            str(result.get("reason")), 404)
        raise AppException(str(result.get("error", "确认失败")),
                           code="pending_confirm_failed", http_status=status)
    name = str(result.get("name", ""))
    # 让下一轮 LLM 知道规则是用户在界面上确认的（会话历史不存 tool 消息，
    # 不补这一条模型会以为规则还没建）
    session.model_messages.append(
        {"role": "user", "content": f"（我已通过界面确认，规则「{name}」已创建生效）"})
    await container.session_store.store_session(session)
    return ApiResponse(data={
        "rule_id": result.get("rule_id"),
        "name": name,
        "summary": str(result.get("summary", "")),
    })


@router.post("/rules/pending/{pending_id}/cancel")
async def cancel_pending_rule(
    pending_id: str,
    payload: PendingConfirmRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """放弃草稿（不落库）。"""
    session, draft_id, entry = await _load_pending_draft(
        container, payload.session_id, pending_id, current_user)
    name = str((entry.get("rule") or {}).get("name", "") or "")
    if not cancel_pending(session, draft_id):
        raise AppException(_PENDING_GONE, code="pending_rule_not_found", http_status=404)
    session.model_messages.append(
        {"role": "user", "content": f"（我取消了规则「{name}」的创建）"})
    await container.session_store.store_session(session)
    return ApiResponse(data={"cancelled": True, "name": name})


@router.post("/rules/pending/{pending_id}/camera")
async def set_pending_rule_camera(
    pending_id: str,
    payload: PendingCameraRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """给待确认草稿改绑摄像头（camera_id="" 表示显式选择「全部摄像头」）。

    渠道无关的通用能力：网页确认弹窗走 confirm 顺带带上 camera_id，无界面渠道
    （飞书问答流程、未来的交互卡片按钮）调这个端点。核心不含任何渠道分支。
    """
    session, draft_id, entry = await _load_pending_draft(
        container, payload.session_id, pending_id, current_user)
    rule = entry.get("rule") if isinstance(entry.get("rule"), dict) else {}
    chosen = _check_camera_choice(container, rule, str(payload.camera_id).strip())
    bound = set_pending_camera(session, draft_id, chosen)
    if not bound.get("ok"):
        raise AppException(str(bound.get("error", _PENDING_GONE)),
                           code="pending_rule_not_found", http_status=404)
    updated = bound.get("rule") or rule
    return ApiResponse(data={"rule": updated, "needs_camera": needs_camera(updated)})


@router.post("/rules/preview")
async def preview_rule(
    payload: RuleCreateRequest,
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """只解析不落库 —— 供 TaskView 在创建前知道这条规则要不要绑摄像头。

    与 POST /task/rule 的区别：那个解析完直接 add_rule，这个只返回预览。
    TaskView 据此决定「直接落库」还是「先弹摄像头选择框」。
    """
    camera_id = str(payload.camera_id or "").strip()
    rule = await container.rule_service.build_rule(
        payload.text, user_id=current_user.get("user_id", ""), camera_id=camera_id)
    condition = str(rule.get("condition", "")).strip()
    if not condition:
        raise AppException("无法从输入中解析出有效的视觉条件",
                           code="rule_parse_failed", http_status=400)
    if camera_id:
        # 调用方已经指定了绑定，同样要校验，别把幻觉 id 带进后续落库
        rule["camera_id"] = _check_camera_choice(container, rule, camera_id)
    return ApiResponse(data={"rule": rule, "needs_camera": needs_camera(rule)})
