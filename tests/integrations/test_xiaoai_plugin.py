"""小爱插件单元测试（mock HA caller，不 spawn、不真连 HA）。

覆盖：自动检测（单台/多台/零台）、显式配置校验、消息 JSON 列表格式、
空结果守卫 + 缓存失效重检、execute_mode、interrupt 降级、YAML 强转边角。
"""

import asyncio
import importlib.util
import json
import types
from pathlib import Path
from unittest.mock import AsyncMock

# 用 importlib 按绝对路径加载，避免 sys.path 污染
# （echo 和 xiaoai 都有 plugin.py，sys.path 模式会互相覆盖）
XIAOAI_DIR = Path(__file__).parent.parent.parent / "integrations" / "xiaoai"
XIAOAI_PLUGIN_PATH = XIAOAI_DIR / "plugin.py"

_spec = importlib.util.spec_from_file_location("xiaoai_plugin", XIAOAI_PLUGIN_PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
XiaoAiPlugin = _module.XiaoAiPlugin

SLUG = "xiaomi_cn_2166464483_lx06"
PLAY_TEXT = f"notify.{SLUG}_play_text_a_5_1"
EXECUTE = f"notify.{SLUG}_execute_text_directive_a_5_5"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _manifest(entity_id="", execute_mode=None):
    m = json.loads((XIAOAI_DIR / "manifest.json").read_text(encoding="utf-8"))
    schema = m["capabilities"][0]["config_schema"]
    schema["entity_id"]["default"] = entity_id
    if execute_mode is not None:
        schema["execute_mode"]["default"] = execute_mode
    return m


def _states(slugs=(SLUG,), with_media_player=True, extra=()):
    """构造 HA states 快照（每台音箱一对 MIoT 规格 notify 实体 + media_player）。"""
    out = []
    for s in slugs:
        out.append({"entity_id": f"notify.{s}_play_text_a_5_1"})
        out.append({"entity_id": f"notify.{s}_execute_text_directive_a_5_5"})
        if with_media_player:
            out.append({"entity_id": f"media_player.{s}"})
    out.extend({"entity_id": e} for e in extra)
    return {"states": out}


def _make_plugin(entity_id="", states=None, call_results=None, execute_mode=None):
    """构造 plugin + mock host。call_results 是 call_service 逐次返回值列表。"""
    plugin = XiaoAiPlugin()
    plugin.host = types.SimpleNamespace(ha=AsyncMock())
    plugin.host.ha.get_states.return_value = (
        states if states is not None else _states())
    if call_results is None:
        plugin.host.ha.call_service.return_value = [{"entity_id": "notify.ok"}]
    else:
        plugin.host.ha.call_service.side_effect = call_results
    plugin.setup(_manifest(entity_id, execute_mode))
    return plugin


# ==================== 自动检测 ====================

def test_speak_auto_detects_single_speaker():
    """未配置 entity_id 时自动检测：单台音箱直接接入 play_text 实体。"""
    plugin = _make_plugin()

    result = _run(plugin.handle("sink.speak", {"text": "床头灯已打开", "msg_id": "m1"}))

    assert result["spoken"] == "床头灯已打开"
    plugin.ha_caller.call_service.assert_awaited_once()
    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["data"]["entity_id"] == PLAY_TEXT
    assert kwargs["data"]["message"] == json.dumps(["床头灯已打开"], ensure_ascii=False)


def test_speak_no_speaker_error():
    """HA 里没有小爱实体：返回可见错误（不再静默假成功）。"""
    plugin = _make_plugin(states={"states": [{"entity_id": "light.bed"}]})

    result = _run(plugin.handle("sink.speak", {"text": "测试"}))

    assert "未发现小爱音箱" in result["error"]
    plugin.ha_caller.call_service.assert_not_awaited()


def test_speak_multiple_speakers_error_lists_candidates():
    """多台小爱：报错列出候选实体，引导手动配置。"""
    plugin = _make_plugin(states=_states(slugs=(SLUG, "xiaomi_cn_123_lx05")))

    result = _run(plugin.handle("sink.speak", {"text": "测试"}))

    assert "2 台小爱音箱" in result["error"]
    assert f"media_player.{SLUG}" in result["error"]
    assert "media_player.xiaomi_cn_123_lx05" in result["error"]
    plugin.ha_caller.call_service.assert_not_awaited()


def test_resolution_cached_until_invalidated():
    """解析结果缓存：两次 speak 只扫一次 states。"""
    plugin = _make_plugin()

    _run(plugin.handle("sink.speak", {"text": "一"}))
    _run(plugin.handle("sink.speak", {"text": "二"}))

    assert plugin.host.ha.get_states.await_count == 1


# ==================== 显式配置 ====================

def test_configured_entity_used_when_verified():
    """显式配置且校验通过：直接使用配置实体，不依赖自动扫描顺序。"""
    plugin = _make_plugin(
        entity_id=f"media_player.{SLUG}",
        states=_states(slugs=(SLUG, "xiaomi_cn_123_lx05")))

    result = _run(plugin.handle("sink.speak", {"text": "测试"}))

    assert result["spoken"] == "测试"
    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["data"]["entity_id"] == PLAY_TEXT


def test_configured_entity_missing_errors_without_fallback():
    """显式配置校验失败：直接报错，不静默回退自动检测（2026-08-17 事故教训）。"""
    plugin = _make_plugin(
        entity_id="media_player.not_exist_lx99",
        states=_states())

    result = _run(plugin.handle("sink.speak", {"text": "测试"}))

    assert "配置的小爱实体不存在" in result["error"]
    assert "notify.not_exist_lx99_play_text_a_5_1" in result["error"]
    plugin.ha_caller.call_service.assert_not_awaited()


# ==================== 消息格式与守卫 ====================

def test_speak_execute_mode_uses_directive_entity():
    """execute_mode=execute：speak 走 execute 实体，两参数 [文本, 非静默]。"""
    plugin = _make_plugin(execute_mode="execute")

    result = _run(plugin.handle("sink.speak", {"text": "播放音乐", "msg_id": "m"}))

    assert result["spoken"] == "播放音乐"
    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["data"]["entity_id"] == EXECUTE
    assert kwargs["data"]["message"] == json.dumps(
        ["播放音乐", False], ensure_ascii=False)


def test_yaml_coercion_edge_text_stays_string():
    """'on' 等会被 YAML 强转的文本经 JSON 列表保持原样（1 参数实体）。"""
    plugin = _make_plugin()

    _run(plugin.handle("sink.speak", {"text": "on"}))

    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["data"]["message"] == '["on"]'


def test_empty_result_guard_invalidates_and_reresolves():
    """call_service 返回 []（实体不存在的静默假成功）：报错 + 失效缓存重扫。"""
    plugin = _make_plugin(call_results=[
        [],                                  # 第一次：守卫触发
        [{"entity_id": "notify.ok"}],        # 第二次：重扫后成功
    ])

    first = _run(plugin.handle("sink.speak", {"text": "一"}))
    assert "实体不存在" in first["error"]

    second = _run(plugin.handle("sink.speak", {"text": "二"}))
    assert second["spoken"] == "二"
    # 守卫触发后重新扫描了 states
    assert plugin.host.ha.get_states.await_count == 2


def test_ha_call_exception_returns_error():
    """HA 调用异常：包装成可见错误，不抛出炸掉 runtime。"""
    plugin = _make_plugin(call_results=RuntimeError("boom"))

    result = _run(plugin.handle("sink.speak", {"text": "测试"}))

    assert "HA 调用失败" in result["error"]


# ==================== interrupt ====================

def test_interrupt_calls_media_stop():
    """interrupt 调 media_player.media_stop 打断播报。"""
    plugin = _make_plugin()

    result = _run(plugin.handle("sink.interrupt", {}))

    assert result["interrupted"] is True
    kwargs = plugin.ha_caller.call_service.call_args.kwargs
    assert kwargs["domain"] == "media_player"
    assert kwargs["entity_id"] == f"media_player.{SLUG}"


def test_interrupt_degrades_without_media_player():
    """无 media_player 实体：跳过 media_stop，interrupt 仍算成功。"""
    plugin = _make_plugin(states=_states(with_media_player=False))

    result = _run(plugin.handle("sink.interrupt", {}))

    assert result["interrupted"] is True
    plugin.ha_caller.call_service.assert_not_awaited()
