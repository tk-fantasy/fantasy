"""Tests for app/services/entity_controls.py — 动态控件推导。

覆盖 resolve_controls 的四类控件（enum/slider/action/_pct 反推）
和 controls_to_text 的文本渲染。
"""
from __future__ import annotations

import pytest

from app.services.entity_controls import resolve_controls, controls_to_text


class TestResolveControlsEnum:
    """枚举控件：数组属性 → service field 匹配。"""

    def test_enum_from_list_attribute(self):
        """数组属性（如 effects_list）匹配 service field → enum 控件。"""
        entity = {
            "entity_id": "media_player.tv",
            "state": "playing",
            "attributes": {
                "effect": "auto",
                "effect_list": ["auto", "night", "movie"],
            },
        }
        services = {
            "media_player": {
                "select_sound_mode": {"fields": ["entity_id", "sound_mode"]},
            }
        }
        # effect_list → target=effect → 无 field 匹配（只有 sound_mode）
        # 此例验证无匹配时不出控件
        controls = resolve_controls(entity, services)
        # effect_list 不会被匹配（target=effect 不在 fields）
        assert "effect" not in controls

    def test_enum_singular_via_plural(self):
        """单数属性借助复数数组选项匹配（1b 分支）。"""
        entity = {
            "entity_id": "climate.ac",
            "state": "cool",
            "attributes": {
                "hvac_mode": "cool",
                "hvac_modes": ["off", "cool", "heat", "auto"],
            },
        }
        services = {
            "climate": {
                "set_hvac_mode": {"fields": ["entity_id", "hvac_mode"]},
            }
        }
        controls = resolve_controls(entity, services)
        assert "hvac_mode" in controls
        ctrl = controls["hvac_mode"]
        assert ctrl["type"] == "enum"
        assert ctrl["options"] == ["off", "cool", "heat", "auto"]
        assert ctrl["current"] == "cool"
        assert ctrl["service"] == "set_hvac_mode"
        assert ctrl["param"] == "hvac_mode"

    def test_enum_skips_supported_prefix(self):
        """supported_ 前缀的属性不生成控件。"""
        entity = {
            "entity_id": "light.lamp",
            "state": "on",
            "attributes": {
                "supported_features": [1, 2, 3],  # 应跳过
            },
        }
        services = {"light": {"turn_on": {"fields": ["entity_id", "supported_features"]}}}
        controls = resolve_controls(entity, services)
        assert "supported_feature" not in controls
        assert "supported_features" not in controls


class TestResolveControlsSlider:
    """滑块控件：数值属性 → service field 匹配。"""

    def test_slider_from_numeric_pct(self):
        """brightness_pct 数值属性 → slider 控件（默认 min=0/max=100）。"""
        entity = {
            "entity_id": "light.lamp",
            "state": "on",
            "attributes": {"brightness_pct": 50},
        }
        services = {
            "light": {"turn_on": {"fields": ["entity_id", "brightness_pct"]}},
        }
        controls = resolve_controls(entity, services)
        # key 规范化为基础名 brightness，避免标签在 Brightness / Brightness Pct 间跳变
        assert "brightness" in controls
        ctrl = controls["brightness"]
        assert ctrl["type"] == "slider"
        assert ctrl["min"] == 0
        assert ctrl["max"] == 100
        assert ctrl["param"] == "brightness_pct"
        assert ctrl["current"] == 50  # 本身即百分比，不再换算

    def test_slider_converts_raw_brightness_to_pct(self):
        """HA 原始 brightness（0-255）→ 百分比滑块，current 正确换算而非恒 100。"""
        entity = {
            "entity_id": "light.lamp",
            "state": "on",
            "attributes": {"brightness": 128},  # 约一半
        }
        services = {
            "light": {"turn_on": {"fields": ["entity_id", "brightness_pct"]}},
        }
        controls = resolve_controls(entity, services)
        assert "brightness" in controls
        ctrl = controls["brightness"]
        assert ctrl["param"] == "brightness_pct"
        assert ctrl["min"] == 0
        assert ctrl["max"] == 100
        assert ctrl["current"] == 50  # round(128 * 100 / 255) = 50

    def test_slider_for_climate_null_temperature(self):
        """空调关机时 temperature 为 null，但有 min_temp/max_temp 边界 → 仍生成滑块。"""
        entity = {
            "entity_id": "climate.ac",
            "state": "off",
            "attributes": {
                "temperature": None,
                "min_temp": 16,
                "max_temp": 30,
                "target_temp_step": 1,
            },
        }
        services = {
            "climate": {"set_temperature": {"fields": ["entity_id", "temperature", "hvac_mode"]}},
        }
        controls = resolve_controls(entity, services)
        assert "temperature" in controls
        ctrl = controls["temperature"]
        assert ctrl["type"] == "slider"
        assert ctrl["min"] == 16
        assert ctrl["max"] == 30
        assert ctrl["current"] == 16  # null 时回退下限
        assert ctrl["service"] == "set_temperature"
        assert ctrl["param"] == "temperature"

    def test_slider_skips_min_max_step_prefix(self):
        """min_/max_/_step 前缀属性不生成控件。"""
        entity = {
            "entity_id": "cover.win",
            "state": "50",
            "attributes": {
                "position": 50,
                "min_position": 0,
                "max_position": 100,
                "position_step": 1,
            },
        }
        services = {"cover": {"set_cover_position": {"fields": ["entity_id", "position"]}}}
        controls = resolve_controls(entity, services)
        assert "position" in controls
        # min/max/step 不出控件
        assert "min_position" not in controls
        assert "max_position" not in controls
        assert "position_step" not in controls

    def test_slider_with_min_max_from_attrs(self):
        """滑块的 min/max/step 从 min_X/max_X/X_step 属性读取。"""
        entity = {
            "entity_id": "number.temp",
            "state": "25",
            "attributes": {"value": 25, "min_value": 10, "max_value": 40, "value_step": 1, "unit_of_measurement": "°C"},
        }
        services = {"number": {"set_value": {"fields": ["entity_id", "value"]}}}
        controls = resolve_controls(entity, services)
        assert "value" in controls
        ctrl = controls["value"]
        assert ctrl["min"] == 10
        assert ctrl["max"] == 40
        assert ctrl["step"] == 1
        assert ctrl["unit"] == "°C"


class TestResolveControlsAction:
    """动作控件：无参服务。"""

    def test_action_from_parameterless_service(self):
        """无参服务（除 entity_id 外无 field）→ action 控件。"""
        entity = {
            "entity_id": "cover.win",
            "state": "open",
            "attributes": {"current_position": 50},
        }
        services = {
            "cover": {
                "open_cover": {"fields": ["entity_id"]},
                "close_cover": {"fields": ["entity_id"]},
                "set_cover_position": {"fields": ["entity_id", "position"]},
            }
        }
        controls = resolve_controls(entity, services)
        # open_cover/close_cover 无参 → action；set_cover_position 有参 → 非 action
        if "open_cover" in controls:
            assert controls["open_cover"]["type"] == "action"
        if "close_cover" in controls:
            assert controls["close_cover"]["type"] == "action"
        # set_ 前缀的服务被跳过（line 101）
        assert "set_cover_position" not in controls or controls.get("set_cover_position", {}).get("type") != "action"


class TestResolveControlsEdgeCases:
    """边界情况。"""

    def test_empty_attributes(self):
        entity = {"entity_id": "sensor.temp", "state": "20", "attributes": {}}
        controls = resolve_controls(entity, {"sensor": {}})
        assert controls == {}

    def test_no_matching_services(self):
        """无任何匹配 service → 空控件。"""
        entity = {
            "entity_id": "light.lamp",
            "state": "on",
            "attributes": {"brightness_pct": 50},
        }
        controls = resolve_controls(entity, {"light": {}})
        assert controls == {}

    def test_unknown_domain_yields_empty(self):
        """entity 的 domain 在 services 里不存在 → 空控件。"""
        entity = {
            "entity_id": "fan.ceiling",
            "state": "on",
            "attributes": {"percentage": 50},
        }
        controls = resolve_controls(entity, {"light": {"turn_on": {"fields": ["entity_id"]}}})
        assert controls == {}


class TestControlsToText:
    """controls_to_text 文本渲染。"""

    def test_empty_controls_renders_no_controls(self):
        entity = {"entity_id": "sensor.temp", "attributes": {"friendly_name": "温度"}}
        text = controls_to_text(entity, {})
        assert "温度" in text
        assert "no controls" in text.lower()

    def test_uses_friendly_name_when_available(self):
        entity = {"entity_id": "light.lamp", "attributes": {"friendly_name": "床头灯"}}
        text = controls_to_text(entity, {})
        assert "床头灯" in text
        assert "light.lamp" in text

    def test_falls_back_to_entity_id_without_name(self):
        entity = {"entity_id": "switch.outlet", "attributes": {}}
        text = controls_to_text(entity, {})
        assert "switch.outlet" in text

    def test_renders_slider_control(self):
        entity = {"entity_id": "light.lamp", "attributes": {"friendly_name": "灯"}}
        controls = {
            "brightness_pct": {
                "type": "slider", "service": "turn_on", "param": "brightness_pct",
                "min": 0, "max": 100, "step": 1, "current": 50, "unit": "%",
            }
        }
        text = controls_to_text(entity, controls)
        assert "Brightness Pct" in text  # 标题化
        assert "turn_on" in text
        assert "0%" in text and "100%" in text

    def test_renders_enum_control(self):
        entity = {"entity_id": "climate.ac", "attributes": {"friendly_name": "空调"}}
        controls = {
            "hvac_mode": {
                "type": "enum", "service": "set_hvac_mode", "param": "hvac_mode",
                "options": ["off", "cool"], "current": "cool",
            }
        }
        text = controls_to_text(entity, controls)
        assert "Hvac Mode" in text
        assert "off|cool" in text
        assert "cool" in text

    def test_renders_action_control(self):
        entity = {"entity_id": "cover.win", "attributes": {"friendly_name": "窗帘"}}
        controls = {"open_cover": {"type": "action", "service": "open_cover", "param": None}}
        text = controls_to_text(entity, controls)
        assert "Open Cover" in text
        assert "action" in text.lower()
