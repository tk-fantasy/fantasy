"""直通退出守护（route_inbound）与 ws 模式解析（_resolve_mode）测试。"""

import asyncio

from app.integration.integration_layer import IntegrationLayer

KEYWORDS_CFG = "app.integration.config_helper.get_config"
SET_MODE = "app.integration.config_helper.set_current_mode"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _make_layer(monkeypatch, keywords=None, set_mode_calls=None):
    if keywords is not None:
        monkeypatch.setattr(KEYWORDS_CFG,
                            lambda path, default=None: keywords if path.startswith(
                                "integration.direct_exit_keywords") else default)
    if set_mode_calls is not None:
        monkeypatch.setattr(SET_MODE,
                            lambda mode: set_mode_calls.append(mode))
    return IntegrationLayer(plugin_dir="nonexistent_dir")


def test_exit_keyword_short_circuit_restores_aether(monkeypatch):
    """短句命中退出词 → 不路由插件，切回 aether 并返回 exited_direct。"""
    set_calls: list[str] = []
    layer = _make_layer(monkeypatch, set_mode_calls=set_calls)

    result = _run(layer.route_inbound("退出直通", "xiaoai_direct"))

    assert result == {"ok": True, "exited_direct": True, "message": "已退出直通模式"}
    assert set_calls == ["aether"]


def test_long_sentence_containing_keyword_passes_through(monkeypatch):
    """长句包含退出词不退出（防误伤），照常走插件路由。"""
    set_calls: list[str] = []
    layer = _make_layer(monkeypatch, set_mode_calls=set_calls)
    long_text = "帮我看看「退出直通」这句话翻译成英文是什么"

    result = _run(layer.route_inbound(long_text, "xiaoai_direct"))

    assert set_calls == []
    assert "exited_direct" not in result
    assert result["ok"] is False  # 无插件可路由


def test_boundary_16_chars_still_exits(monkeypatch):
    """恰好 16 字的短句命中退出词仍退出。"""
    set_calls: list[str] = []
    layer = _make_layer(monkeypatch, set_mode_calls=set_calls)
    text = "好了好了，请退出直通模式吧"  # len == 16（含逗号）

    result = _run(layer.route_inbound(text, "xiaoai_direct"))

    assert result.get("exited_direct") is True
    assert set_calls == ["aether"]


def test_custom_keywords_via_config(monkeypatch):
    """词表可配：配置里的自定义词同样生效。"""
    set_calls: list[str] = []
    layer = _make_layer(monkeypatch, keywords=["小爱退下"], set_mode_calls=set_calls)

    result = _run(layer.route_inbound("小爱退下", "xiaoai_direct"))

    assert result.get("exited_direct") is True
    assert set_calls == ["aether"]


def test_aether_mode_skips_guard(monkeypatch):
    """mode=aether 不进守护（普通路由查找，无插件时报 no inbound router）。"""
    set_calls: list[str] = []
    layer = _make_layer(monkeypatch, set_mode_calls=set_calls)

    result = _run(layer.route_inbound("退出直通", "aether"))

    assert set_calls == []
    assert result["ok"] is False
    assert "no inbound router" in result["error"]


def test_resolve_mode_payload_explicit_wins(monkeypatch):
    """前端显式传非默认模式 → 照用，不查全局。"""
    import app.routes.ws_routes as ws

    assert ws._resolve_mode("xiaoai_direct") == "xiaoai_direct"


def test_resolve_mode_default_falls_back_to_current_mode(monkeypatch):
    """前端传默认 aether → 回退查宿主 current_mode（LLM 切模式对路由生效）。"""
    monkeypatch.setattr("app.integration.config_helper.get_current_mode",
                        lambda: "xiaoai_direct")
    import app.routes.ws_routes as ws

    assert ws._resolve_mode("aether") == "xiaoai_direct"
    assert ws._resolve_mode("") == "xiaoai_direct"


def test_resolve_mode_falls_back_to_aether_on_error(monkeypatch):
    """config_helper 异常（集成配置不可用）→ 安全回退 aether。"""
    def _boom():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("app.integration.config_helper.get_current_mode", _boom)
    import app.routes.ws_routes as ws

    assert ws._resolve_mode("aether") == "aether"
