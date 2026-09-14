"""小爱插件 xiaoai_direct_mode 工具测试（不 spawn，fake 反向 RPC）。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from app.integration.rpc_protocol import (
    METHOD_HOST_HA_STATES,
    METHOD_HOST_MODE_GET,
    METHOD_HOST_MODE_SET,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
)
from integrations.xiaoai.plugin import XiaoAiPlugin

MANIFEST = json.loads(
    (Path(__file__).resolve().parents[2] / "integrations" / "xiaoai" / "manifest.json")
    .read_text(encoding="utf-8")
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_plugin(states=None, mode_get="aether"):
    """构造 setup 完成的小爱插件；host 反向调用按 method 记录/应答。"""
    calls: list[tuple] = []

    async def host_call(method, params=None):
        calls.append((method, params or {}))
        if method == METHOD_HOST_HA_STATES:
            return states if states is not None else {"states": [
                {"entity_id": "notify.s_play_text_a_5_1"},
                {"entity_id": "notify.s_execute_text_directive_a_5_5"},
                {"entity_id": "media_player.s"},
            ]}
        if method == METHOD_HOST_MODE_SET:
            return {"mode": params.get("mode")}
        if method == METHOD_HOST_MODE_GET:
            return {"mode": mode_get}
        raise RuntimeError(f"unexpected method: {method}")

    plugin = XiaoAiPlugin()
    plugin.host = type("H", (), {"ha": AsyncMock(), "mode": type("M", (), {})()})()
    # 用真实 HostProxy 语义：直接挂 host_call
    from app.integration.sdk.plugin_base import HostProxy
    plugin.host = HostProxy(host_call)
    plugin.setup(MANIFEST)
    return plugin, calls


def test_manifest_declares_agent_tools_and_mode_permission():
    """manifest 声明 agent_tools capability + mode 权限（装配与反向 RPC 的前提）。"""
    types = [c["type"] for c in MANIFEST["capabilities"]]
    assert "agent_tools" in types
    assert "mode" in MANIFEST["permissions"]


def test_setup_injects_direct_mode_tool():
    plugin, _ = _make_plugin()
    assert len(plugin.tools) == 1
    assert plugin.tools[0].name == "xiaoai_direct_mode"


def test_tools_list_via_handle():
    plugin, _ = _make_plugin()
    result = _run(plugin.handle(METHOD_TOOLS_LIST, {}))
    assert [t["name"] for t in result["tools"]] == ["xiaoai_direct_mode"]


def test_enter_sets_direct_mode_after_speaker_check():
    plugin, calls = _make_plugin()

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {
        "name": "xiaoai_direct_mode", "arguments": {"action": "enter"},
    }))

    assert result["ok"] is True
    assert result["mode"] == "xiaoai_direct"
    assert result["speaker"] == "s"
    assert (METHOD_HOST_MODE_SET, {"mode": "xiaoai_direct"}) in calls


def test_enter_refused_when_speaker_unavailable():
    """音箱解析失败（离线/未接入）→ 报错且不切模式。"""
    plugin, calls = _make_plugin(states={"states": []})

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {
        "name": "xiaoai_direct_mode", "arguments": {"action": "enter"},
    }))

    assert "error" in result
    assert not any(m == METHOD_HOST_MODE_SET for m, _ in calls)


def test_exit_is_idempotent_and_sets_aether():
    plugin, calls = _make_plugin(mode_get="aether")

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {
        "name": "xiaoai_direct_mode", "arguments": {"action": "exit"},
    }))

    assert result["ok"] is True
    assert result["mode"] == "aether"
    assert (METHOD_HOST_MODE_SET, {"mode": "aether"}) in calls


def test_status_reports_current_mode_without_switching():
    plugin, calls = _make_plugin(mode_get="xiaoai_direct")

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {
        "name": "xiaoai_direct_mode", "arguments": {"action": "status"},
    }))

    assert result == {"ok": True, "mode": "xiaoai_direct", "speaker": "s"}
    assert not any(m == METHOD_HOST_MODE_SET for m, _ in calls)


def test_unknown_action_returns_error():
    plugin, _ = _make_plugin()

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {
        "name": "xiaoai_direct_mode", "arguments": {"action": "boom"},
    }))

    assert "error" in result
    assert "hint" in result
