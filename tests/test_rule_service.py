"""Tests for RuleService pure methods."""
from __future__ import annotations

from app.services.rule_service import RuleService


class TestParseJson:
    def setup_method(self):
        self.svc = RuleService.__new__(RuleService)

    def test_valid(self):
        result = self.svc._parse_json('{"condition": "test"}')
        assert result == {"condition": "test"}

    def test_invalid(self):
        result = self.svc._parse_json("not json")
        assert result == {}


class TestParseHaCatalog:
    def setup_method(self):
        self.svc = RuleService.__new__(RuleService)

    def test_basic(self):
        catalog = "- light.bed (类型:light, 状态:on) 名称:床头灯\n- light.kitchen (类型:light, 状态:off) 名称:厨房灯"
        devices = self.svc._parse_ha_catalog(catalog)
        assert len(devices) == 2
        assert devices[0]["entity_id"] == "light.bed"
        assert devices[0]["name"] == "床头灯"

    def test_empty(self):
        assert self.svc._parse_ha_catalog("") == []
        assert self.svc._parse_ha_catalog("(暂无 HA 设备)") == []


class TestFindMatchingEntity:
    def setup_method(self):
        self.svc = RuleService.__new__(RuleService)
        self.devices = [
            {"entity_id": "light.chuang_tou_deng", "name": "床头灯", "domain": "light"},
            {"entity_id": "light.chu_fang_deng", "name": "厨房灯", "domain": "light"},
            {"entity_id": "climate.ke_ting_kong_tiao", "name": "客厅空调", "domain": "climate"},
        ]

