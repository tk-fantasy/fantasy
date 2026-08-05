"""Tests for app/services/control_probe.py — 报错探测保底机制。"""
from __future__ import annotations

import pytest
import httpx

from app.services.control_probe import (
    call_with_probe,
    ControlProbeCache,
    _single_numeric_param,
    _normalize_to_range,
    _probe_cache,
)


# ===== 辅助：构造 mock ha_client 和 httpx.HTTPStatusError =====

def _make_400_error() -> httpx.HTTPStatusError:
    """构造一个 400 的 HTTPStatusError，用于模拟 HA 拒绝越界值。"""
    req = httpx.Request("POST", "http://ha/api/services/media_player/volume_set")
    resp = httpx.Response(400, request=req)
    return httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)


class MockHaClient:
    """可编程的 mock ha_client。

    用 responses 队列模拟 call_service 的返回/抛异常：
    - 每次调用消费队列首元素
    - 元素是 dict（成功返回）或 Exception（抛出）
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # 记录所有调用参数

    async def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append({"domain": domain, "service": service,
                           "entity_id": entity_id, "data": dict(data) if data else {}})
        if not self.responses:
            return {"ok": True}
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def clear_cache():
    """每个测试前清空全局探测缓存，避免相互影响。"""
    _probe_cache.clear()
    yield
    _probe_cache.clear()


# ===== _single_numeric_param 单元测试 =====

class TestSingleNumericParam:
    def test_single_number(self):
        assert _single_numeric_param({"volume_level": 50}) == ("volume_level", 50.0)

    def test_single_int(self):
        assert _single_numeric_param({"brightness": 128}) == ("brightness", 128.0)

    def test_none_data(self):
        assert _single_numeric_param(None) is None

    def test_empty_data(self):
        assert _single_numeric_param({}) is None

    def test_multi_param(self):
        assert _single_numeric_param({"a": 1, "b": 2}) is None

    def test_bool_excluded(self):
        """bool 是 int 子类但不是滑块值，应排除。"""
        assert _single_numeric_param({"is_muted": True}) is None

    def test_string_value(self):
        assert _single_numeric_param({"mode": "auto"}) is None


# ===== _normalize_to_range 单元测试 =====

class TestNormalizeToRange:
    def test_value_in_range(self):
        assert _normalize_to_range(0.5, (0.0, 1.0)) == 0.5

    def test_50_to_0_1_range(self):
        """用户传 50（以为是 0-100），实际范围 0-1 → 归一化为 0.5。"""
        result = _normalize_to_range(50, (0.0, 1.0))
        assert result == pytest.approx(0.5)

    def test_100_to_0_1_range(self):
        result = _normalize_to_range(100, (0.0, 1.0))
        assert result == pytest.approx(1.0)

    def test_0_stays_0(self):
        assert _normalize_to_range(0, (0.0, 1.0)) == 0.0


# ===== call_with_probe 集成测试 =====

class TestCallWithProbe:
    @pytest.mark.asyncio
    async def test_success_first_try_no_probe(self):
        """正常调用成功 → 不触发探测。"""
        client = MockHaClient([{"ok": True}])
        result = await call_with_probe(
            client, "media_player", "volume_set", "media_player.spk",
            {"volume_level": 0.5},
        )
        assert result == {"ok": True}
        assert len(client.calls) == 1  # 只调用一次
        assert client.calls[0]["data"] == {"volume_level": 0.5}

    @pytest.mark.asyncio
    async def test_400_triggers_probe_finds_0_1(self):
        """传 50 返回 400 → 探测 0.5 成功 → 缓存 → 用用户原值重发。

        调用序列：
        1. 用户原值 50 → 400（mock 队列第1个）
        2. 探测中点 0.5 → 成功（mock 队列第2个）
        3. 用户原值归一化后 0.5 重发 → 成功（mock 队列第3个）
        """
        client = MockHaClient([
            _make_400_error(),   # 用户传 50 → 400
            {"ok": True},        # 探测 0.5 → 成功
            {"ok": True},        # 归一化重发 → 成功
        ])
        result = await call_with_probe(
            client, "media_player", "volume_set", "media_player.spk",
            {"volume_level": 50},
        )
        assert result == {"ok": True}
        assert len(client.calls) == 3
        # 第3次调用应是归一化后的值（50 → 0.5）
        assert client.calls[2]["data"]["volume_level"] == pytest.approx(0.5)
        # 探测结果已缓存
        assert _probe_cache.get("media_player.spk", "volume_level") == (0.0, 1.0)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_probe(self):
        """缓存命中 → 直接归一化，不触发首次 400。"""
        _probe_cache.set("media_player.spk", "volume_level", (0.0, 1.0))
        client = MockHaClient([{"ok": True}])
        result = await call_with_probe(
            client, "media_player", "volume_set", "media_player.spk",
            {"volume_level": 50},  # 用户传 0-100 刻度的值
        )
        assert result == {"ok": True}
        assert len(client.calls) == 1  # 只一次，直接归一化
        assert client.calls[0]["data"]["volume_level"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_500_not_probed(self):
        """500（非范围错误）→ 不探测，直接抛出。"""
        req = httpx.Request("POST", "http://ha/api/services/media_player/turn_on")
        resp_500 = httpx.Response(500, request=req)
        err_500 = httpx.HTTPStatusError("500", request=req, response=resp_500)
        client = MockHaClient([err_500])
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_probe(
                client, "media_player", "turn_on", "media_player.spk", None,
            )
        assert len(client.calls) == 1  # 无探测调用

    @pytest.mark.asyncio
    async def test_no_data_passthrough(self):
        """无 data（action 调用）→ 原样透传，不探测。"""
        client = MockHaClient([{"ok": True}])
        result = await call_with_probe(
            client, "media_player", "play_media", "media_player.spk", None,
        )
        assert result == {"ok": True}
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_probe_all_fail_reraises(self):
        """探测全失败（所有候选刻度都 400）→ 抛出原始 400。"""
        client = MockHaClient([
            _make_400_error(),   # 用户原值
            _make_400_error(),   # 探测 0-1 刻度
            _make_400_error(),   # 探测 0-100 刻度
            _make_400_error(),   # 探测 0-255 刻度
        ])
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_probe(
                client, "number", "set_value", "number.x", {"value": 9999},
            )
        # 无缓存
        assert _probe_cache.get("number.x", "value") is None
