"""Tests for rule_routes.py - 规则 CRUD。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestRuleRoutes:
    """测试规则管理路由。"""

    @pytest.mark.asyncio
    async def test_list_rules(self):
        """列出规则返回规则列表。"""
        from app.routes.rule_routes import list_rules

        mock_container = MagicMock()
        mock_container.rule_registry_service.list_rules.return_value = [
            {"id": "rule-1", "name": "测试规则"}
        ]

        result = await list_rules(container=mock_container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_delete_rule(self):
        """删除规则成功。"""
        from app.routes.rule_routes import delete_rule

        mock_container = MagicMock()
        mock_container.rule_registry_service.delete_rule.return_value = True

        result = await delete_rule("rule-123", container=mock_container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_set_rule_enabled(self):
        """切换规则启用状态。"""
        from app.routes.rule_routes import set_rule_enabled
        from app.schema.api_schemas import RuleEnabledRequest

        mock_container = MagicMock()
        mock_container.rule_registry_service.set_enabled.return_value = True

        payload = RuleEnabledRequest(enabled=True)
        result = await set_rule_enabled("rule-123", payload, container=mock_container)
        assert result.code == "ok"


class TestBuildRuleRoute:
    """测试 /api/task/rule 路由。"""

    @pytest.mark.asyncio
    async def test_build_rule_success(self):
        """创建规则任务成功，user_id 从 current_user 注入。"""
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "测试规则",
            "condition": "晚上",
            "actions": [],
        })
        mock_container.rule_registry_service.add_rule.return_value = {
            "id": "rule-1",
            "name": "测试规则",
        }

        payload = RuleCreateRequest(text="晚上开灯")
        current_user = {"user_id": "u1", "username": "alice"}
        result = await build_rule(payload, container=mock_container, current_user=current_user)
        assert result.code == "ok"

        # build_rule 必须带 user_id + camera_id(D7:空串=全局规则)
        mock_container.rule_service.build_rule.assert_awaited_once_with("晚上开灯", user_id="u1", camera_id="")
        # add_rule 是同步方法，必须带 user_id（持久化到 rules.user_id 列）
        mock_container.rule_registry_service.add_rule.assert_called_once()
        assert mock_container.rule_registry_service.add_rule.call_args.kwargs.get("user_id") == "u1"

    @pytest.mark.asyncio
    async def test_build_rule_no_condition_raises_400(self):
        """LLM 解析不出 condition → 抛 400，不调 add_rule。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "x", "condition": "", "actions": [],
        })

        payload = RuleCreateRequest(text="无效输入")
        current_user = {"user_id": "u1", "username": "alice"}
        # 此前是 return ApiResponse(success=False, ...)，但 ApiResponse 没有 success
        # 字段 → 静默变成 code="ok" + HTTP 200，前端把失败当成功解包
        with pytest.raises(AppException) as ei:
            await build_rule(payload, container=mock_container, current_user=current_user)
        assert ei.value.http_status == 400
        assert "无法从输入中解析出" in ei.value.message
        mock_container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_build_rule_passes_camera_id(self):
        """D7:RuleCreateRequest.camera_id 透传给 build_rule(空串=全局,绑某路=该路)。"""
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "门口规则", "condition": "有人", "actions": [],
        })
        mock_container.rule_registry_service.add_rule.return_value = {"id": "r2"}

        # 选某路 → camera_id 绑该摄像头
        payload = RuleCreateRequest(text="门口有人", camera_id="cam_abc123")
        current_user = {"user_id": "u1", "username": "alice"}
        await build_rule(payload, container=mock_container, current_user=current_user)
        mock_container.rule_service.build_rule.assert_awaited_once_with(
            "门口有人", user_id="u1", camera_id="cam_abc123")


class TestCreateRuleRoute:
    """测试 POST /api/rules 路由（手动构造规则，不走 LLM）。"""

    @pytest.mark.asyncio
    async def test_create_rule_injects_user_id(self):
        """create_rule 必须把 current_user.user_id 传给 add_rule。"""
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        mock_container = MagicMock()
        mock_container.rule_registry_service.add_rule.return_value = {"id": "r1"}

        payload = RulePayloadRequest(condition="晚上10点后", actions=[], type="time")
        current_user = {"user_id": "u2", "username": "bob"}
        result = await create_rule(payload, container=mock_container, current_user=current_user)
        assert result.code == "ok"
        mock_container.rule_registry_service.add_rule.assert_called_once()
        assert mock_container.rule_registry_service.add_rule.call_args.kwargs.get("user_id") == "u2"

    @pytest.mark.asyncio
    async def test_create_rule_vision_without_camera_rejected(self):
        """视觉规则不绑摄像头 → 400。否则产出的就是 ruleMismatch 标红的危险规则。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        mock_container = MagicMock()
        payload = RulePayloadRequest(condition="画面里有人", actions=[])

        with pytest.raises(AppException) as ei:
            await create_rule(payload, container=mock_container,
                              current_user={"user_id": "u2", "username": "bob"})

        assert ei.value.http_status == 400
        assert ei.value.code == "camera_required"
        mock_container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_rule_vision_explicit_global_allowed(self):
        """camera_id="" 是「全部摄像头」的显式选择，必须放行。"""
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        mock_container = MagicMock()
        mock_container.camera_manager.list_cameras = MagicMock(
            return_value=[{"id": "cam_1", "name": "门口"}])
        mock_container.rule_registry_service.add_rule.return_value = {"id": "r1"}

        payload = RulePayloadRequest(condition="画面里有人", actions=[], camera_id="")
        result = await create_rule(payload, container=mock_container,
                                   current_user={"user_id": "u2", "username": "bob"})

        assert result.code == "ok"
        assert mock_container.rule_registry_service.add_rule.call_args.args[0]["camera_id"] == ""

    @pytest.mark.asyncio
    async def test_create_rule_drops_unset_optional_fields(self):
        """扩字段不能改变旧调用方的行为：没传的 name/summary 不该以 None 落进 add_rule。"""
        from app.routes.rule_routes import create_rule
        from app.schema.api_schemas import RulePayloadRequest

        mock_container = MagicMock()
        mock_container.rule_registry_service.add_rule.return_value = {"id": "r1"}

        await create_rule(RulePayloadRequest(condition="晚上10点后", actions=[], type="time"),
                          container=mock_container,
                          current_user={"user_id": "u2", "username": "bob"})

        stored = mock_container.rule_registry_service.add_rule.call_args.args[0]
        assert "name" not in stored
        assert "summary" not in stored
        assert "camera_id" not in stored
        assert stored["enabled"] is True


# ---------------------------------------------------------------------------
# 待确认草稿端点 — 网页确认弹窗直接落库，绕过模型
# ---------------------------------------------------------------------------

DRAFT_RULE = {
    "name": "有人开研发部灯",
    "condition": "画面里有人",
    # 视觉规则且已绑定摄像头 —— 即用户在弹窗里选过之后的状态。
    # 不写 type 会被 is_vision_rule 兜底成 vision，再缺 camera_id 就撞上 confirm
    # 的「视觉规则必须指定摄像头」强校验，后面这些用例全测不到自己本来要测的东西。
    "type": "vision",
    "camera_id": "cam_1",
    "actions": [{"mcp_tool_name": "ha_devices___call_service",
                 "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                    "entity_id": "light.rd"}}],
    "summary": "有人就打开研发部灯",
}

ALICE = {"user_id": "u1", "username": "alice"}


def _session_with_draft(user_id="u1", pending_id="p1", age=0.0, rule=None):
    """真实 SessionState（不是 mock）—— 要断言 model_messages 的追加与顺序。"""
    import time as _time

    from app.services.session_store import SessionState

    session = SessionState(session_id="s1", request_id="r1", user_id=user_id)
    session.model_messages = [
        {"role": "user", "content": "如果有人就打开研发部灯"},
        {"role": "assistant", "content": "规则尚未创建，请确认后生效"},
    ]
    session.pending_confirmations[pending_id] = {
        "kind": "automation_rule",
        "rule": dict(rule or DRAFT_RULE),
        "created_at": _time.time() - age,
    }
    return session


CAMERAS = [{"id": "cam_1", "name": "研发部"}, {"id": "cam_2", "name": "门口"}]


def _container_with(session, ha_states=None, add_rule_fail=False, cameras=CAMERAS):
    mock_container = MagicMock()
    mock_container.session_store.get_session = AsyncMock(return_value=session)
    mock_container.session_store.store_session = AsyncMock()
    if add_rule_fail:
        mock_container.rule_registry_service.add_rule.side_effect = RuntimeError("db locked")
    else:
        mock_container.rule_registry_service.add_rule.return_value = {
            **DRAFT_RULE, "id": "rule-1", "enabled": True,
        }
    mock_container.ha_service.get_states_snapshot = AsyncMock(
        return_value=[{"entity_id": "light.rd"}] if ha_states is None else ha_states)
    mock_container.ha_client_ref = [MagicMock()]
    # cameras=None 模拟 camera_manager 未装配（校验放行）；[] 模拟一路都没有
    if cameras is not None:
        mock_container.camera_manager.list_cameras = MagicMock(return_value=cameras)
    else:
        mock_container.camera_manager = None
    return mock_container


class TestPendingRuleExplain:
    """POST /api/rules/pending/{pending_id}/explain"""

    @pytest.mark.asyncio
    async def test_explain_uses_draft_rule_not_request_body(self):
        from app.routes.rule_routes import explain_pending_rule
        from app.schema.api_schemas import PendingExplainRequest

        session = _session_with_draft()
        container = _container_with(session)
        container.rule_service.explain_rule = AsyncMock(return_value="有人就开灯")

        result = await explain_pending_rule(
            "p1", PendingExplainRequest(session_id="s1", question="啥时候触发？"),
            container=container, current_user=ALICE)

        assert result.data == {"answer": "有人就开灯"}
        # 规则取自草稿，前端不需要（也不该）传 current
        assert container.rule_service.explain_rule.await_args.args[0]["name"] == "有人开研发部灯"
        assert container.rule_service.explain_rule.await_args.kwargs["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_explain_expired_draft_404(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import explain_pending_rule
        from app.schema.api_schemas import PendingExplainRequest

        session = _session_with_draft(age=601)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await explain_pending_rule(
                "p1", PendingExplainRequest(session_id="s1", question="？"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 404
        assert "过期" in exc.value.message

    @pytest.mark.asyncio
    async def test_explain_other_users_session_403(self):
        """草稿按 session 存，必须校验归属 —— 否则任何人拿到 session_id 就能读他人规则。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import explain_pending_rule
        from app.schema.api_schemas import PendingExplainRequest

        session = _session_with_draft(user_id="someone-else")
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await explain_pending_rule(
                "p1", PendingExplainRequest(session_id="s1", question="？"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 403
        container.rule_service.explain_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_explain_missing_session_404(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import explain_pending_rule
        from app.schema.api_schemas import PendingExplainRequest

        container = MagicMock()
        container.session_store.get_session = AsyncMock(return_value=None)

        with pytest.raises(AppException) as exc:
            await explain_pending_rule(
                "p1", PendingExplainRequest(session_id="nope", question="？"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 404


class TestPendingRuleRevise:
    """POST /api/rules/pending/{pending_id}/revise"""

    @pytest.mark.asyncio
    async def test_revise_writes_back_to_draft_and_resets_ttl(self):
        import time as _time

        from app.routes.rule_routes import revise_pending_rule
        from app.schema.api_schemas import PendingReviseRequest

        session = _session_with_draft(age=300)
        container = _container_with(session)
        container.rule_service.revise_rule = AsyncMock(return_value={
            "rule": {**DRAFT_RULE, "condition": "画面里有两个人"},
            "summary": "改成两个人",
        })

        result = await revise_pending_rule(
            "p1", PendingReviseRequest(session_id="s1", instruction="要两个人才开"),
            container=container, current_user=ALICE)

        assert result.data["rule"]["condition"] == "画面里有两个人"
        assert result.data["summary"] == "改成两个人"
        # 草稿被就地更新，且重新计时给用户完整评估窗口
        draft = session.pending_confirmations["p1"]
        assert draft["rule"]["condition"] == "画面里有两个人"
        assert _time.time() - draft["created_at"] < 5
        # 草稿不持久化，改草稿不需要落库
        container.session_store.store_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revise_llm_failure_raises(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import revise_pending_rule
        from app.schema.api_schemas import PendingReviseRequest

        session = _session_with_draft()
        container = _container_with(session)
        container.rule_service.revise_rule = AsyncMock(side_effect=RuntimeError("llm down"))

        with pytest.raises(AppException) as exc:
            await revise_pending_rule(
                "p1", PendingReviseRequest(session_id="s1", instruction="改"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 502
        assert "llm down" in exc.value.message

    @pytest.mark.asyncio
    async def test_revise_with_stale_id_falls_back_to_only_draft(self):
        from app.routes.rule_routes import revise_pending_rule
        from app.schema.api_schemas import PendingReviseRequest

        session = _session_with_draft()
        container = _container_with(session)
        container.rule_service.revise_rule = AsyncMock(return_value={
            "rule": DRAFT_RULE, "summary": "ok"})

        result = await revise_pending_rule(
            "编出来的id", PendingReviseRequest(session_id="s1", instruction="改"),
            container=container, current_user=ALICE)

        assert result.code == "ok"


class TestPendingRuleConfirm:
    """POST /api/rules/pending/{pending_id}/confirm"""

    @pytest.mark.asyncio
    async def test_confirm_saves_pops_draft_and_records_in_history(self):
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft()
        container = _container_with(session)

        result = await confirm_pending_rule(
            "p1", PendingConfirmRequest(session_id="s1"),
            container=container, current_user=ALICE)

        assert result.data == {"rule_id": "rule-1", "name": "有人开研发部灯",
                               "summary": "有人就打开研发部灯"}
        assert container.rule_registry_service.add_rule.call_args.kwargs["user_id"] == "u1"
        assert session.pending_confirmations == {}
        container.session_store.store_session.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_confirm_appends_after_existing_history(self):
        """顺序回归：确认消息必须排在原始请求之后。

        dispatcher 在轮末才 append user/assistant，前端因此要等 Dialog.Finish
        再弹确认框；这里锁住"追加在尾部"这个前提，别让确认先于请求进历史。
        """
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft()
        container = _container_with(session)

        await confirm_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                   container=container, current_user=ALICE)

        roles = [m["content"] for m in session.model_messages]
        assert roles[0] == "如果有人就打开研发部灯"
        assert roles[-1] == "（我已通过界面确认，规则「有人开研发部灯」已创建生效）"
        assert len(roles) == 3

    @pytest.mark.asyncio
    async def test_confirm_blocked_when_entity_gone(self):
        """设备已消失 → 400 且不落库，草稿留着让用户改。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft()
        container = _container_with(session, ha_states=[])

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                       container=container, current_user=ALICE)

        assert exc.value.http_status == 400
        assert "light.rd" in exc.value.message
        container.rule_registry_service.add_rule.assert_not_called()
        assert "p1" in session.pending_confirmations
        container.session_store.store_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_save_failure_keeps_draft(self):
        """落库抛错是服务端问题（500），且草稿必须留着让用户重试。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft()
        container = _container_with(session, add_rule_fail=True)

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                       container=container, current_user=ALICE)

        assert exc.value.http_status == 500
        assert "db locked" in exc.value.message
        assert "p1" in session.pending_confirmations
        container.session_store.store_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_expired_draft_404(self):
        """进程重启后草稿必丢（不持久化），前端要把这句话显示出来而不是静默。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(age=601)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                       container=container, current_user=ALICE)

        assert exc.value.http_status == 404
        assert "重新描述需求" in exc.value.message
        container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_other_users_session_403(self):
        """规则落库后会真实驱动设备，跨用户确认必须挡住。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(user_id="someone-else")
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                       container=container, current_user=ALICE)

        assert exc.value.http_status == 403
        container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_without_id_uses_only_draft(self):
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft()
        container = _container_with(session)

        result = await confirm_pending_rule(
            "已失效的id", PendingConfirmRequest(session_id="s1"),
            container=container, current_user=ALICE)

        assert result.data["rule_id"] == "rule-1"


class TestPendingRuleCancel:
    """POST /api/rules/pending/{pending_id}/cancel"""

    @pytest.mark.asyncio
    async def test_cancel_pops_draft_and_records_in_history(self):
        from app.routes.rule_routes import cancel_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft()
        container = _container_with(session)

        result = await cancel_pending_rule(
            "p1", PendingConfirmRequest(session_id="s1"),
            container=container, current_user=ALICE)

        assert result.data == {"cancelled": True, "name": "有人开研发部灯"}
        assert session.pending_confirmations == {}
        assert session.model_messages[-1]["content"] == "（我取消了规则「有人开研发部灯」的创建）"
        container.rule_registry_service.add_rule.assert_not_called()
        container.session_store.store_session.assert_awaited_once_with(session)

    @pytest.mark.asyncio
    async def test_cancel_expired_draft_404(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import cancel_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(age=601)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await cancel_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                      container=container, current_user=ALICE)

        assert exc.value.http_status == 404
        container.session_store.store_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_other_users_session_403(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import cancel_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(user_id="someone-else")
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await cancel_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                      container=container, current_user=ALICE)

        assert exc.value.http_status == 403
        assert "p1" in session.pending_confirmations


# ---------------------------------------------------------------------------
# 摄像头绑定 — 视觉规则必须显式选一路（或显式选全局），不许静默落成危险的全局规则
# ---------------------------------------------------------------------------

UNBOUND_VISION = {**DRAFT_RULE, "camera_id": ""}
WEATHER_RULE = {**DRAFT_RULE, "type": "weather", "condition": "下雨", "camera_id": ""}


class TestPendingRuleCameraBinding:
    """confirm 的 camera_id 三态：None=没选 / ""=显式全局 / 非空=绑定某路。"""

    @pytest.mark.asyncio
    async def test_confirm_vision_without_choice_rejected(self):
        """没选摄像头就想确认视觉规则 → 400，不落库。前端禁用按钮只是体验，
        真正的不变量守在服务端。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule("p1", PendingConfirmRequest(session_id="s1"),
                                       container=container, current_user=ALICE)

        assert exc.value.http_status == 400
        assert exc.value.code == "camera_required"
        container.rule_registry_service.add_rule.assert_not_called()
        assert "p1" in session.pending_confirmations  # 草稿留着让用户选

    @pytest.mark.asyncio
    async def test_confirm_vision_with_camera_binds_it(self):
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session)

        result = await confirm_pending_rule(
            "p1", PendingConfirmRequest(session_id="s1", camera_id="cam_2"),
            container=container, current_user=ALICE)

        assert result.data["rule_id"] == "rule-1"
        saved = container.rule_registry_service.add_rule.call_args.args[0]
        assert saved["camera_id"] == "cam_2"

    @pytest.mark.asyncio
    async def test_confirm_explicit_global_allowed(self):
        """camera_id="" 是「全部摄像头」的显式选择，必须放行（合法用法）。"""
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session)

        result = await confirm_pending_rule(
            "p1", PendingConfirmRequest(session_id="s1", camera_id=""),
            container=container, current_user=ALICE)

        assert result.data["rule_id"] == "rule-1"
        assert container.rule_registry_service.add_rule.call_args.args[0]["camera_id"] == ""

    @pytest.mark.asyncio
    async def test_confirm_unknown_camera_lists_candidates(self):
        """幻觉/失效的 camera_id → 400，message 里带上可选项供自纠。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule(
                "p1", PendingConfirmRequest(session_id="s1", camera_id="cam_nope"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 400
        assert exc.value.code == "camera_not_found"
        assert "研发部" in exc.value.message and "门口" in exc.value.message
        container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_vision_with_no_cameras_rejected(self):
        """一路摄像头都没有：视觉规则永远不会触发，建了比不建更糟。"""
        from app.core.exceptions import AppException
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session, cameras=[])

        with pytest.raises(AppException) as exc:
            await confirm_pending_rule(
                "p1", PendingConfirmRequest(session_id="s1", camera_id=""),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 400
        assert exc.value.code == "no_camera_available"
        container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_nonvision_needs_no_camera(self):
        """定时/天气规则不依赖摄像头，不选也能落库。"""
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=WEATHER_RULE)
        container = _container_with(session, cameras=[])

        result = await confirm_pending_rule(
            "p1", PendingConfirmRequest(session_id="s1"),
            container=container, current_user=ALICE)

        assert result.data["rule_id"] == "rule-1"

    @pytest.mark.asyncio
    async def test_confirm_camera_manager_missing_fails_open(self):
        """camera_manager 未装配（异构装配/测试桩）时不锁死创建。"""
        from app.routes.rule_routes import confirm_pending_rule
        from app.schema.api_schemas import PendingConfirmRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session, cameras=None)

        result = await confirm_pending_rule(
            "p1", PendingConfirmRequest(session_id="s1", camera_id="cam_anything"),
            container=container, current_user=ALICE)

        assert result.data["rule_id"] == "rule-1"


class TestSetPendingCameraEndpoint:
    """POST /api/rules/pending/{pending_id}/camera — 渠道无关的通用改绑端点。"""

    @pytest.mark.asyncio
    async def test_binds_and_reports_needs_camera(self):
        from app.routes.rule_routes import set_pending_rule_camera
        from app.schema.api_schemas import PendingCameraRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session)

        result = await set_pending_rule_camera(
            "p1", PendingCameraRequest(session_id="s1", camera_id="cam_2"),
            container=container, current_user=ALICE)

        assert result.data["needs_camera"] is False
        assert result.data["rule"]["camera_id"] == "cam_2"
        # 写回草稿本身，后续 confirm 直接用它
        assert session.pending_confirmations["p1"]["rule"]["camera_id"] == "cam_2"
        container.rule_registry_service.add_rule.assert_not_called()
        container.session_store.store_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unbind_to_global(self):
        from app.routes.rule_routes import set_pending_rule_camera
        from app.schema.api_schemas import PendingCameraRequest

        session = _session_with_draft()  # DRAFT_RULE 已绑 cam_1
        container = _container_with(session)

        result = await set_pending_rule_camera(
            "p1", PendingCameraRequest(session_id="s1", camera_id=""),
            container=container, current_user=ALICE)

        assert result.data["rule"]["camera_id"] == ""
        # 视觉规则解绑后又回到缺摄像头状态，前端据此重新挡住确认
        assert result.data["needs_camera"] is True

    @pytest.mark.asyncio
    async def test_unknown_camera_400(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import set_pending_rule_camera
        from app.schema.api_schemas import PendingCameraRequest

        session = _session_with_draft(rule=UNBOUND_VISION)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await set_pending_rule_camera(
                "p1", PendingCameraRequest(session_id="s1", camera_id="cam_nope"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 400
        assert "p1" in session.pending_confirmations

    @pytest.mark.asyncio
    async def test_expired_draft_404(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import set_pending_rule_camera
        from app.schema.api_schemas import PendingCameraRequest

        session = _session_with_draft(age=601, rule=UNBOUND_VISION)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await set_pending_rule_camera(
                "p1", PendingCameraRequest(session_id="s1", camera_id="cam_2"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 404

    @pytest.mark.asyncio
    async def test_other_users_session_403(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import set_pending_rule_camera
        from app.schema.api_schemas import PendingCameraRequest

        session = _session_with_draft(user_id="someone-else", rule=UNBOUND_VISION)
        container = _container_with(session)

        with pytest.raises(AppException) as exc:
            await set_pending_rule_camera(
                "p1", PendingCameraRequest(session_id="s1", camera_id="cam_2"),
                container=container, current_user=ALICE)

        assert exc.value.http_status == 403


class TestRulePreview:
    """POST /api/rules/preview — 只解析不落库，供 TaskView 决定是否弹选择框。"""

    @pytest.mark.asyncio
    async def test_preview_vision_reports_needs_camera(self):
        from app.routes.rule_routes import preview_rule
        from app.schema.api_schemas import RuleCreateRequest

        container = MagicMock()
        container.rule_service.build_rule = AsyncMock(return_value=dict(UNBOUND_VISION))

        result = await preview_rule(RuleCreateRequest(text="有人就开灯"),
                                    container=container, current_user=ALICE)

        assert result.data["needs_camera"] is True
        assert result.data["rule"]["condition"] == "画面里有人"
        container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_preview_weather_needs_no_camera(self):
        from app.routes.rule_routes import preview_rule
        from app.schema.api_schemas import RuleCreateRequest

        container = MagicMock()
        container.rule_service.build_rule = AsyncMock(return_value=dict(WEATHER_RULE))

        result = await preview_rule(RuleCreateRequest(text="下雨就关窗"),
                                    container=container, current_user=ALICE)

        assert result.data["needs_camera"] is False

    @pytest.mark.asyncio
    async def test_preview_passes_camera_id_and_validates(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import preview_rule
        from app.schema.api_schemas import RuleCreateRequest

        container = MagicMock()
        container.rule_service.build_rule = AsyncMock(return_value=dict(UNBOUND_VISION))
        container.camera_manager.list_cameras = MagicMock(return_value=CAMERAS)

        result = await preview_rule(RuleCreateRequest(text="有人就开灯", camera_id="cam_2"),
                                    container=container, current_user=ALICE)
        assert result.data["rule"]["camera_id"] == "cam_2"
        assert result.data["needs_camera"] is False
        container.rule_service.build_rule.assert_awaited_once_with(
            "有人就开灯", user_id="u1", camera_id="cam_2")

        with pytest.raises(AppException) as exc:
            await preview_rule(RuleCreateRequest(text="有人就开灯", camera_id="cam_nope"),
                               container=container, current_user=ALICE)
        assert exc.value.code == "camera_not_found"

    @pytest.mark.asyncio
    async def test_preview_unparsable_400(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import preview_rule
        from app.schema.api_schemas import RuleCreateRequest

        container = MagicMock()
        container.rule_service.build_rule = AsyncMock(return_value={"condition": "  "})

        with pytest.raises(AppException) as exc:
            await preview_rule(RuleCreateRequest(text="乱写"),
                               container=container, current_user=ALICE)

        assert exc.value.http_status == 400
        container.rule_registry_service.add_rule.assert_not_called()



class TestBuildRuleAutoRepairGating:
    """幻觉设备自动修复后的零匹配拦截：validation_errors 不出库（与聊天工具同口径）。"""

    @pytest.mark.asyncio
    async def test_build_rule_rejects_unmatched_devices(self):
        from app.core.exceptions import AppException
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "开大门", "condition": "有人", "type": "vision",
            "actions": [{"mcp_tool_name": "ha_devices___call_service",
                         "mcp_tool_input": {"domain": "cover", "service": "open_cover",
                                            "entity_id": "cover.front_door", "data": {}}}],
            "validation_errors": ["动作1: entity_id 'cover.front_door' 不存在"],
        })

        payload = RuleCreateRequest(text="创建规则：有人就开大门")
        with pytest.raises(AppException) as exc_info:
            await build_rule(payload, container=mock_container,
                             current_user={"user_id": "u1", "username": "alice"})

        assert exc_info.value.http_status == 400
        assert "cover.front_door" in exc_info.value.message or "设备" in exc_info.value.message
        mock_container.rule_registry_service.add_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_build_rule_strips_validation_errors_on_success(self):
        """修复干净的规则落库时不应残留 validation_errors 字段。"""
        from app.routes.rule_routes import build_rule
        from app.schema.api_schemas import RuleCreateRequest

        mock_container = MagicMock()
        mock_container.rule_service.build_rule = AsyncMock(return_value={
            "name": "开大门", "condition": "有人", "type": "vision",
            "actions": [{"mcp_tool_name": "ha_devices___call_service",
                         "mcp_tool_input": {"domain": "switch", "service": "turn_on",
                                            "entity_id": "switch.da_men", "data": {}}}],
            "auto_corrections": [{"action_index": 0, "from": "cover.front_door",
                                  "to": "switch.da_men", "to_name": "大门开关"}],
        })
        mock_container.rule_registry_service.add_rule.return_value = {"id": "r9"}

        await build_rule(RuleCreateRequest(text="创建规则：有人就开大门"),
                         container=mock_container, current_user={"user_id": "u1"})

        saved = mock_container.rule_registry_service.add_rule.call_args.args[0]
        assert "validation_errors" not in saved
        assert saved["auto_corrections"][0]["to_name"] == "大门开关"
