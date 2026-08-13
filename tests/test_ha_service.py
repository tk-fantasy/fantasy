"""Tests for HAService with mocked HomeAssistantClient."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ha_service import HAService


def _make_service(devices: list[dict] | None = None) -> tuple[HAService, MagicMock]:
    client = MagicMock()
    client.get_states = AsyncMock(return_value=devices or [])
    client._base_url = "http://localhost:8123"
    client._token = "test-token"
    return HAService(client=client), client


class TestHAService:
    @pytest.mark.asyncio
    async def test_get_all_devices_without_area_filtered(self):
        """没 area_id 的设备被过滤（避免 HA 内置实体涌入设备列表）。"""
        devices = [
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "Bed Light"}},
            {"entity_id": "climate.ac", "state": "cool", "attributes": {"friendly_name": "AC"}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {}
        svc._entity_area_map = {}
        svc._area_cache_at = 9999999999
        result = await svc.get_all_devices()
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_all_devices_filters_non_device_domains(self):
        """sun/zone/person 等 HA 内置 domain 即使有 area_id 也不出现。"""
        devices = [
            {"entity_id": "light.bed", "state": "on", "attributes": {}},
            {"entity_id": "sun.sun", "state": "above_horizon", "attributes": {}},
            {"entity_id": "zone.home", "state": "0", "attributes": {}},
            {"entity_id": "person.admin", "state": "home", "attributes": {}},
            {"entity_id": "update.ha_os", "state": "off", "attributes": {}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {"bedroom": "Bedroom", "home": "Home"}
        svc._entity_area_map = {
            "light.bed": "bedroom",
            "sun.sun": "bedroom",      # 有 area 但 domain 不在白名单
            "zone.home": "home",
            "person.admin": "home",
            "update.ha_os": "home",
        }
        svc._area_cache_at = 9999999999
        result = await svc.get_all_devices()
        ids = [d["entity_id"] for d in result]
        assert ids == ["light.bed"]

    @pytest.mark.asyncio
    async def test_get_all_devices_with_area(self):
        devices = [
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "Bed Light"}},
        ]
        svc, _ = _make_service(devices)
        # Mock area maps
        svc._area_map = {"bedroom": "Bedroom"}
        svc._entity_area_map = {"light.bed": "bedroom"}
        svc._area_cache_at = 9999999999
        result = await svc.get_all_devices()
        assert len(result) == 1
        assert result[0]["entity_id"] == "light.bed"
        assert result[0]["area_name"] == "Bedroom"

    @pytest.mark.asyncio
    async def test_empty_devices(self):
        svc, _ = _make_service([])
        svc._area_map = {}
        svc._entity_area_map = {}
        svc._area_cache_at = 9999999999
        result = await svc.get_all_devices()
        assert result == []


class TestInvalidateStatesCache:
    """测试缓存失效逻辑。"""

    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self):
        """invalidate_states_cache 后，下次 _get_states_cached 重新拉取。"""
        svc, client = _make_service([
            {"entity_id": "light.bed", "state": "on", "attributes": {}},
        ])
        # 第一次拉取 → client.get_states 被调用一次
        await svc._get_states_cached()
        assert client.get_states.call_count == 1

        # 在 TTL 内再拉 → 命中缓存，不重新请求
        await svc._get_states_cached()
        assert client.get_states.call_count == 1

        # 失效缓存 → 再次拉取会重新请求 HA
        svc.invalidate_states_cache()
        await svc._get_states_cached()
        assert client.get_states.call_count == 2


class TestVirtualSuppress:
    """模拟器设备「全部离线才隐藏」过滤规则。"""

    SIM_IDS = [
        "light.chuang_tou_deng", "climate.zhong_yang_kong_diao",
        "cover.ke_ting_chuang_lian", "sensor.ke_ting_wen_du",
    ]

    def _states_by_id(self, overrides: dict[str, str]) -> dict[str, dict]:
        """构造 states_by_id：默认全部 on，overrides 覆盖指定 entity 的 state。"""
        return {
            eid: {"entity_id": eid, "state": overrides.get(eid, "on"), "attributes": {}}
            for eid in self.SIM_IDS
        }

    def test_all_unavailable_suppresses_all(self, monkeypatch):
        """白名单全 unavailable → 全部隐藏。"""
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: self.SIM_IDS if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        states = self._states_by_id({eid: "unavailable" for eid in self.SIM_IDS})
        assert svc._virtual_suppress_set(states) == set(self.SIM_IDS)

    def test_partial_online_suppresses_none(self, monkeypatch):
        """有 1 个在线 → 一个都不隐藏。"""
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: self.SIM_IDS if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        states = self._states_by_id({"light.chuang_tou_deng": "on"})  # 其余 unavailable
        assert svc._virtual_suppress_set(states) == set()

    def test_empty_whitelist_suppresses_none(self, monkeypatch):
        """白名单为空 → 不隐藏任何设备（特性关闭）。"""
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: [] if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        states = self._states_by_id({eid: "unavailable" for eid in self.SIM_IDS})
        assert svc._virtual_suppress_set(states) == set()

    def test_unregistered_entity_ignored(self, monkeypatch):
        """白名单实体不在 states（未注册）→ 忽略它；其余全 unavailable 仍触发。"""
        only_two = ["light.chuang_tou_deng", "climate.zhong_yang_kong_diao"]
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: self.SIM_IDS if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        # states 里只有 2 个，且都 unavailable
        states = {eid: {"entity_id": eid, "state": "unavailable", "attributes": {}} for eid in only_two}
        assert svc._virtual_suppress_set(states) == set(only_two)

    @pytest.mark.asyncio
    async def test_flat_devices_excluded_when_all_offline(self, monkeypatch):
        """get_all_devices：模拟器全 offline 时，flat 列表不含模拟器实体。"""
        sim_ids = ["light.chuang_tou_deng", "climate.zhong_yang_kong_diao"]
        real_id = "light.real_bed"
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: sim_ids if path == "simulator.entity_ids" else default,
        )
        devices = [
            {"entity_id": "light.chuang_tou_deng", "state": "unavailable", "attributes": {}},
            {"entity_id": "climate.zhong_yang_kong_diao", "state": "unavailable", "attributes": {}},
            {"entity_id": "light.real_bed", "state": "on", "attributes": {}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {"bedroom": "Bedroom"}
        svc._entity_area_map = {eid: "bedroom" for eid in sim_ids + [real_id]}
        svc._area_cache_at = 9999999999

        result = await svc.get_all_devices()
        ids = [d["entity_id"] for d in result]
        assert ids == ["light.real_bed"]  # 模拟器两个被隐藏，真实设备保留

    @pytest.mark.asyncio
    async def test_flat_devices_kept_when_partial_online(self, monkeypatch):
        """get_all_devices：模拟器有 1 个在线时，全部保留。"""
        sim_ids = ["light.chuang_tou_deng", "climate.zhong_yang_kong_diao"]
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: sim_ids if path == "simulator.entity_ids" else default,
        )
        devices = [
            {"entity_id": "light.chuang_tou_deng", "state": "on", "attributes": {}},
            {"entity_id": "climate.zhong_yang_kong_diao", "state": "unavailable", "attributes": {}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {"bedroom": "Bedroom"}
        svc._entity_area_map = {eid: "bedroom" for eid in sim_ids}
        svc._area_cache_at = 9999999999

        result = await svc.get_all_devices()
        ids = [d["entity_id"] for d in result]
        assert set(ids) == set(sim_ids)  # 一个都不隐藏
