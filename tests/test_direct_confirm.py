"""dispatcher 确定性确认短路测试 — 裸「确认」+ 唯一活草稿 → 直接落库+确定性回复。

弱模型在确认轮高频幻觉「已生效」却不调 automation_rule_confirm（实测），
短路把确认收口成系统动作：与网页 REST 确认同一 confirm_pending；命中时返回
确定性回复文本，调用方跳过 agent（模型连转述的机会都没有）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.dispatcher import Dispatcher
from app.services.pending_rules import KIND_AUTOMATION_RULE


def _dispatcher() -> Dispatcher:
    return Dispatcher(session_store=MagicMock(), agent=MagicMock(),
                      camera_manager=MagicMock(), ha_catalog_provider=lambda: "")


def _session_with_draft(delegated=False):
    rule = {"name": "早晨开灯", "condition": "每天早上7点半", "type": "time",
            "actions": [{"mcp_tool_name": "ha_devices___call_service",
                         "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                            "entity_id": "light.rd"}}],
            "summary": "每天早上7点半开研发部灯"}
    entry = {"kind": KIND_AUTOMATION_RULE, "rule": rule, "created_at": time.time()}
    if delegated:
        entry["ha_trigger"] = {"trigger": [{"platform": "time", "at": "07:30:00"}],
                               "condition": []}
    return SimpleNamespace(user_id="u1", model_messages=[],
                           pending_confirmations={"p1": entry})


def _container_stub(delegated=False):
    c = MagicMock()
    c.rule_registry_service.add_rule.return_value = {
        "name": "早晨开灯", "id": "rule-1", "enabled": True,
        "summary": "每天早上7点半开研发部灯",
        "trigger_source": "ha" if delegated else "",
        "ha_automation_id": "aid-1" if delegated else "",
    }
    c.ha_client_ref = [MagicMock()]
    c.ha_service.get_states_snapshot = AsyncMock(return_value=[{"entity_id": "light.rd"}])
    if delegated:
        c.ha_automation_service.write_delegated = AsyncMock(
            return_value={"id": "aid-1", "alias": "Aether·x"})
    return c


class TestMaybeDirectConfirm:
    async def test_bare_confirm_saves_and_returns_reply(self):
        d = _dispatcher()
        session = _session_with_draft(delegated=True)
        with patch("app.container.get_container", return_value=_container_stub(delegated=True)):
            reply = await d._maybe_direct_confirm(session, "确认", user_id="u1")

        assert reply is not None
        assert "已确认并创建规则「早晨开灯」" in reply
        assert "Home Assistant" in reply  # 委托版注明触发归 HA
        assert "p1" not in session.pending_confirmations  # 草稿已摘
        # 回复由调用方落历史，钩子自身不动 model_messages
        assert session.model_messages == []

    async def test_delegated_off_reply_plain(self):
        d = _dispatcher()
        session = _session_with_draft()
        with patch("app.container.get_container", return_value=_container_stub(delegated=False)):
            reply = await d._maybe_direct_confirm(session, "好的", user_id="u1")

        assert reply is not None
        assert "Home Assistant" not in reply
        assert "自动评估" in reply

    async def test_non_bare_query_returns_none(self):
        """带其他内容的「确认」不短路（可能另有所指）。"""
        d = _dispatcher()
        session = _session_with_draft()
        with patch("app.container.get_container") as gc:
            assert await d._maybe_direct_confirm(session, "确认下今天天气", user_id="u1") is None
            gc.assert_not_called()
        assert "p1" in session.pending_confirmations

    async def test_no_draft_returns_none(self):
        d = _dispatcher()
        session = SimpleNamespace(user_id="u1", model_messages=[],
                                  pending_confirmations={})
        with patch("app.container.get_container") as gc:
            assert await d._maybe_direct_confirm(session, "确认", user_id="u1") is None
            gc.assert_not_called()

    async def test_vision_rule_missing_camera_skipped(self):
        """视觉规则缺摄像头绑定不能替用户选路——留给模型追问。"""
        d = _dispatcher()
        session = _session_with_draft()
        session.pending_confirmations["p1"]["rule"] = {
            "name": "有人开灯", "condition": "画面里有人", "type": "vision",
            "camera_id": "", "actions": []}
        with patch("app.container.get_container") as gc:
            assert await d._maybe_direct_confirm(session, "确认", user_id="u1") is None
            gc.assert_not_called()
        assert "p1" in session.pending_confirmations

    async def test_confirm_failure_returns_none(self):
        """落库失败回退模型原流程（None），草稿保留。"""
        d = _dispatcher()
        session = _session_with_draft()
        c = _container_stub()
        c.rule_registry_service.add_rule.side_effect = RuntimeError("db locked")
        with patch("app.container.get_container", return_value=c):
            assert await d._maybe_direct_confirm(session, "确认", user_id="u1") is None
        assert "p1" in session.pending_confirmations  # confirm_pending 失败不摘草稿


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
