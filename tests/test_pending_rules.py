"""services.pending_rules 测试 — 两段式确认草稿的共享存取/落库逻辑。

网页确认弹窗的 REST 端点与聊天工具 automation_rule_* 共用这一层，所以这里锁的是
两条入口都必须遵守的口径：TTL 懒过期、唯一草稿兜底、实体校验放行、落库失败不摘草稿。
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.services.pending_rules import (
    KIND_AUTOMATION_RULE,
    PENDING_TTL_SECONDS,
    cancel_pending,
    confirm_pending,
    find_missing_entities,
    get_live_pending,
    is_vision_rule,
    locate_pending,
    needs_camera,
    pending_store,
    resolve_pending,
    set_pending_camera,
)

RULE = {
    "name": "有人开研发部灯",
    "condition": "画面里有人",
    # 已绑定摄像头的视觉规则。缺 type 会被 is_vision_rule 兜底成 vision，再缺
    # camera_id 就撞上 confirm_pending 的 camera_required —— 那会让下面这些用例
    # 全都测不到自己本来要测的分支（实体缺失 / 落库失败 / 单草稿兜底）。
    "type": "vision",
    "camera_id": "cam_1",
    "actions": [{"mcp_tool_name": "ha_devices___call_service",
                 "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                    "entity_id": "light.rd"}}],
    "summary": "有人就打开研发部灯",
}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _session(**drafts):
    return SimpleNamespace(user_id="u1", pending_confirmations=dict(drafts))


def _draft(rule=None, age=0.0, kind=KIND_AUTOMATION_RULE):
    return {"kind": kind, "rule": rule if rule is not None else dict(RULE),
            "created_at": time.time() - age}


class StubRegistry:
    def __init__(self, fail=False):
        self.saved: list[dict] = []
        self.fail = fail

    def add_rule(self, rule, user_id=""):
        if self.fail:
            raise RuntimeError("db locked")
        saved = {**rule, "id": "rule-1", "user_id": user_id, "enabled": True}
        self.saved.append(saved)
        return saved


class StubHA:
    def __init__(self, states):
        self._states = states

    async def get_states_snapshot(self):
        if isinstance(self._states, Exception):
            raise self._states
        return self._states


class StubClient:
    async def get_states(self):
        return []


# ---------------------------------------------------------------------------
# pending_store / get_live_pending
# ---------------------------------------------------------------------------

def test_pending_store_inits_missing_field():
    """旧反序列化会话对象没有该字段时补上（草稿不持久化，重启后必然缺）。"""
    session = SimpleNamespace(user_id="u1")

    store = pending_store(session)

    assert store == {}
    assert session.pending_confirmations is store


def test_pending_store_tolerates_readonly_session():
    """只读桩上 setattr 失败也不能炸，退化为返回一个临时 dict。"""
    class _Readonly:
        @property
        def pending_confirmations(self):
            raise AttributeError("readonly")

        def __setattr__(self, name, value):
            raise AttributeError("readonly")

    assert pending_store(_Readonly()) == {}


def test_get_live_pending_rejects_wrong_kind():
    session = _session(p1=_draft(kind="scheduled_task"))

    assert get_live_pending(session, "p1", KIND_AUTOMATION_RULE) is None
    assert session.pending_confirmations["p1"] is not None  # 类型不符不删


def test_get_live_pending_lazily_expires():
    session = _session(p1=_draft(age=PENDING_TTL_SECONDS + 1))

    assert get_live_pending(session, "p1", KIND_AUTOMATION_RULE) is None
    assert "p1" not in session.pending_confirmations


def test_get_live_pending_returns_fresh_entry():
    session = _session(p1=_draft())

    entry = get_live_pending(session, "p1", KIND_AUTOMATION_RULE)

    assert entry is session.pending_confirmations["p1"]


# ---------------------------------------------------------------------------
# resolve_pending / locate_pending — 口头确认路径的兜底
# ---------------------------------------------------------------------------

def test_resolve_pending_single_draft():
    session = _session(p1=_draft())

    pid, entry, count = resolve_pending(session, KIND_AUTOMATION_RULE)

    assert (pid, count) == ("p1", 1)
    assert entry["rule"]["name"] == RULE["name"]


def test_resolve_pending_ambiguous_returns_count_only():
    session = _session(p1=_draft(), p2=_draft())

    pid, entry, count = resolve_pending(session, KIND_AUTOMATION_RULE)

    assert (pid, entry, count) == (None, None, 2)


def test_resolve_pending_sweeps_expired_and_ignores_other_kinds():
    session = _session(
        gone=_draft(age=PENDING_TTL_SECONDS + 1),
        task=_draft(kind="scheduled_task"),
        live=_draft(),
    )

    pid, _entry, count = resolve_pending(session, KIND_AUTOMATION_RULE)

    assert (pid, count) == ("live", 1)
    assert "gone" not in session.pending_confirmations   # 过期已清
    assert "task" in session.pending_confirmations       # 别的 kind 不动


def test_resolve_pending_skips_malformed_entries():
    session = _session(bad="not-a-dict", good=_draft())

    pid, _entry, count = resolve_pending(session, KIND_AUTOMATION_RULE)

    assert (pid, count) == ("good", 1)


def test_locate_pending_prefers_explicit_id():
    session = _session(p1=_draft(rule={**RULE, "name": "甲"}),
                       p2=_draft(rule={**RULE, "name": "乙"}))

    pid, entry, err = locate_pending(session, "p2", KIND_AUTOMATION_RULE)

    assert (pid, err) == ("p2", "")
    assert entry["rule"]["name"] == "乙"


def test_locate_pending_falls_back_when_id_missing():
    """跨轮后模型丢了 pending_id（会话历史不存 tool 消息），唯一草稿自动采用。"""
    session = _session(p1=_draft())

    pid, entry, err = locate_pending(session, "", KIND_AUTOMATION_RULE)

    assert (pid, err) == ("p1", "")
    assert entry is not None


def test_locate_pending_falls_back_when_id_stale():
    session = _session(p1=_draft())

    pid, _entry, err = locate_pending(session, "hallucinated", KIND_AUTOMATION_RULE)

    assert (pid, err) == ("p1", "")


def test_locate_pending_ambiguous_requires_user_to_choose():
    session = _session(p1=_draft(), p2=_draft())

    pid, entry, err = locate_pending(session, "", KIND_AUTOMATION_RULE)

    assert (pid, entry) == (None, None)
    assert "2 个" in err


def test_locate_pending_stale_id_with_no_drafts_reports_expiry():
    session = _session()

    _pid, _entry, err = locate_pending(session, "p1", KIND_AUTOMATION_RULE)

    assert "过期" in err


def test_locate_pending_no_id_and_no_drafts():
    _pid, _entry, err = locate_pending(_session(), "", KIND_AUTOMATION_RULE)

    assert "没有待确认" in err


# ---------------------------------------------------------------------------
# find_missing_entities
# ---------------------------------------------------------------------------

def test_find_missing_entities_flags_gone_device():
    missing = _run(find_missing_entities(
        RULE, StubHA([{"entity_id": "light.other"}]), [StubClient()]))

    assert missing == ["light.rd"]


def test_find_missing_entities_passes_when_present():
    missing = _run(find_missing_entities(
        RULE, StubHA([{"entity_id": "light.rd"}]), [StubClient()]))

    assert missing == []


def test_find_missing_entities_skips_rules_without_entity():
    rule = {"actions": [{"mcp_tool_name": "notify", "mcp_tool_input": {}}]}

    assert _run(find_missing_entities(rule, StubHA([]), [StubClient()])) == []


def test_find_missing_entities_fails_open_when_ha_unreachable():
    """校验本身不可用时放行 —— 与 call_service 的口径一致，不因检查挂掉锁死创建。"""
    class _NoSnapshot:
        async def boom(self):
            raise AssertionError("不该被调用")

    class _BadClient:
        async def get_states(self):
            raise RuntimeError("ha down")

    missing = _run(find_missing_entities(RULE, _NoSnapshot(), [_BadClient()]))

    assert missing == []


# ---------------------------------------------------------------------------
# confirm_pending / cancel_pending
# ---------------------------------------------------------------------------

def test_confirm_pending_saves_and_pops_draft():
    registry = StubRegistry()
    session = _session(p1=_draft())

    result = _run(confirm_pending(session, "p1", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()],
                                  user_id="u9"))

    assert result["ok"] is True
    assert result["rule_id"] == "rule-1"
    assert result["name"] == RULE["name"]
    assert registry.saved[0]["user_id"] == "u9"
    assert "p1" not in session.pending_confirmations


def test_confirm_pending_blocks_on_missing_entity_and_keeps_draft():
    """设备已消失 → 拒绝落库，但草稿留着让用户改（revise 换设备）。"""
    registry = StubRegistry()
    session = _session(p1=_draft())

    result = _run(confirm_pending(session, "p1", registry, StubHA([]), [StubClient()]))

    assert result["ok"] is False
    assert result["reason"] == "missing_entities"
    assert result["missing_entities"] == ["light.rd"]
    assert "light.rd" in result["error"]
    assert registry.saved == []
    assert "p1" in session.pending_confirmations


def test_confirm_pending_keeps_draft_when_save_fails():
    """落库抛错时不能把草稿摘掉，否则用户重试无门。"""
    session = _session(p1=_draft())

    result = _run(confirm_pending(session, "p1", StubRegistry(fail=True),
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()]))

    assert result["ok"] is False
    assert result["reason"] == "save_failed"
    assert "db locked" in result["error"]
    assert "p1" in session.pending_confirmations


def test_confirm_pending_without_id_uses_single_draft():
    registry = StubRegistry()
    session = _session(p1=_draft())

    result = _run(confirm_pending(session, "", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()]))

    assert result["ok"] is True
    assert result["pending_id"] == "p1"


def test_confirm_pending_no_draft():
    result = _run(confirm_pending(_session(), "p1", StubRegistry(),
                                  StubHA([]), [StubClient()]))

    assert result["ok"] is False
    assert "过期" in result["error"]


def test_cancel_pending_pops_draft():
    session = _session(p1=_draft())

    assert cancel_pending(session, "p1") is True
    assert "p1" not in session.pending_confirmations


def test_cancel_pending_unknown_id_is_false():
    assert cancel_pending(_session(), "nope") is False


def test_cancel_pending_falls_back_to_single_draft():
    session = _session(p1=_draft())

    assert cancel_pending(session, "") is True
    assert session.pending_confirmations == {}


# ---------------------------------------------------------------------------
# is_vision_rule / needs_camera / set_pending_camera — 摄像头绑定
# ---------------------------------------------------------------------------

def test_is_vision_rule_treats_missing_or_bad_type_as_vision():
    """兜底方向与 rule_service 一致：宁可多问一次绑哪路，也不让规则静默失效。"""
    assert is_vision_rule({"type": "vision"}) is True
    assert is_vision_rule({}) is True
    assert is_vision_rule({"type": "  "}) is True
    assert is_vision_rule({"type": "乱填的"}) is True
    assert is_vision_rule({"type": "VISION"}) is True


def test_is_vision_rule_false_for_time_and_weather():
    assert is_vision_rule({"type": "time"}) is False
    assert is_vision_rule({"type": "weather"}) is False


def test_needs_camera_only_for_unbound_vision():
    assert needs_camera({"type": "vision", "camera_id": ""}) is True
    assert needs_camera({"type": "vision"}) is True
    assert needs_camera({"type": "vision", "camera_id": "  "}) is True
    assert needs_camera({"type": "vision", "camera_id": "cam_1"}) is False
    # 非视觉规则带不带摄像头都不算「缺」（带了是 orange 错配，另一回事）
    assert needs_camera({"type": "weather", "camera_id": ""}) is False
    assert needs_camera({"type": "time", "camera_id": "cam_1"}) is False


def test_set_pending_camera_binds_and_resets_ttl():
    session = _session(p1=_draft(rule={"type": "vision", "camera_id": ""}, age=300))

    result = set_pending_camera(session, "p1", "cam_2")

    assert result["ok"] is True
    assert result["pending_id"] == "p1"
    assert result["rule"]["camera_id"] == "cam_2"
    assert needs_camera(session.pending_confirmations["p1"]["rule"]) is False
    # 与 revise 同口径：改完重新计时，给用户完整评估窗口
    assert time.time() - session.pending_confirmations["p1"]["created_at"] < 5


def test_set_pending_camera_empty_means_explicit_global():
    session = _session(p1=_draft(rule={"type": "vision", "camera_id": "cam_1"}))

    result = set_pending_camera(session, "p1", "")

    assert result["ok"] is True
    assert result["rule"]["camera_id"] == ""
    # 解绑后视觉规则又回到缺摄像头状态，调用方据此重新挡住确认
    assert needs_camera(result["rule"]) is True


def test_set_pending_camera_strips_whitespace():
    session = _session(p1=_draft(rule={"type": "vision", "camera_id": ""}))

    assert set_pending_camera(session, "p1", "  cam_2  ")["rule"]["camera_id"] == "cam_2"


def test_set_pending_camera_unknown_draft():
    result = set_pending_camera(_session(), "nope", "cam_1")

    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_set_pending_camera_falls_back_to_single_draft():
    session = _session(p1=_draft(rule={"type": "vision", "camera_id": ""}))

    result = set_pending_camera(session, "", "cam_2")

    assert result["ok"] is True
    assert result["pending_id"] == "p1"


def test_set_pending_camera_malformed_draft():
    session = _session(p1={"kind": KIND_AUTOMATION_RULE, "rule": "不是 dict",
                           "created_at": time.time()})

    result = set_pending_camera(session, "p1", "cam_1")

    assert result["ok"] is False
    assert result["reason"] == "malformed"


def test_confirm_pending_after_binding_saves_with_camera():
    """绑定 → 确认：落库的规则带着 camera_id，automation_service 只评估那一路。"""
    registry = StubRegistry()
    session = _session(p1=_draft(rule={**RULE, "type": "vision", "camera_id": ""}))
    set_pending_camera(session, "p1", "cam_2")

    result = _run(confirm_pending(session, "p1", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()]))

    assert result["ok"] is True
    assert registry.saved[0]["camera_id"] == "cam_2"


# ---------------------------------------------------------------------------
# confirm_pending 的摄像头不变量 —— 网页(REST)与语音(工具)两条路都汇到这里，
# 只在路由层校验的话工具路径能绕过去落库一条未绑定的全局视觉规则
# ---------------------------------------------------------------------------

CAM_LIST = [{"id": "cam_1", "name": "研发部"}, {"id": "cam_2", "name": "门口"}]


def test_confirm_unbound_vision_rejected():
    registry = StubRegistry()
    session = _session(p1=_draft(rule={**RULE, "camera_id": ""}))

    result = _run(confirm_pending(session, "p1", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()],
                                  known_cameras=CAM_LIST))

    assert result["ok"] is False
    assert result["reason"] == "camera_required"
    assert result["candidates"] == ["研发部", "门口"]
    assert registry.saved == []
    assert "p1" in session.pending_confirmations  # 草稿留着，等用户指定


def test_confirm_camera_invariant_holds_without_camera_list():
    """不变量不依赖是否知道有哪些摄像头，候选名单只影响提示语。"""
    registry = StubRegistry()
    session = _session(p1=_draft(rule={**RULE, "camera_id": ""}))

    result = _run(confirm_pending(session, "p1", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()]))

    assert result["ok"] is False
    assert result["reason"] == "camera_required"
    assert result["candidates"] == []
    assert registry.saved == []


def test_confirm_explicit_global_allowed_after_choice():
    """显式选「全部摄像头」（camera_id="")合法 —— 靠 camera_chosen 与"没选"区分。"""
    registry = StubRegistry()
    session = _session(p1=_draft(rule={**RULE, "camera_id": ""}))
    set_pending_camera(session, "p1", "")

    result = _run(confirm_pending(session, "p1", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()],
                                  known_cameras=CAM_LIST))

    assert result["ok"] is True
    assert registry.saved[0]["camera_id"] == ""


def test_set_pending_camera_marks_choice():
    session = _session(p1=_draft(rule={**RULE, "camera_id": ""}))

    set_pending_camera(session, "p1", "")

    assert session.pending_confirmations["p1"]["camera_chosen"] is True


def test_confirm_nonvision_needs_no_camera():
    registry = StubRegistry()
    session = _session(p1=_draft(rule={**RULE, "type": "weather", "camera_id": ""}))

    result = _run(confirm_pending(session, "p1", registry,
                                  StubHA([{"entity_id": "light.rd"}]), [StubClient()]))

    assert result["ok"] is True


def test_confirm_missing_entity_checked_after_camera():
    """摄像头校验在实体校验之前：两个都不满足时先报摄像头（用户能立刻补救）。"""
    registry = StubRegistry()
    session = _session(p1=_draft(rule={**RULE, "camera_id": ""}))

    result = _run(confirm_pending(session, "p1", registry, StubHA([]), [StubClient()],
                                  known_cameras=CAM_LIST))

    assert result["reason"] == "camera_required"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
