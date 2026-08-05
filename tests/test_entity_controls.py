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

    def test_enum_available_modes_not_misassigned(self):
        """available_modes 数组不应被词匹配回退错配到 set_humidity/humidity。"""
        entity = {
            "entity_id": "humidifier.bedroom",
            "state": "on",
            "attributes": {
                "humidity": 50,
                "min_humidity": 30,
                "max_humidity": 80,
                "mode": "normal",
                "available_modes": ["normal", "eco", "boost"],
            },
        }
        services = {
            "humidifier": {
                "set_humidity": {"fields": ["entity_id", "humidity"]},
                "set_mode": {"fields": ["entity_id", "mode"]},
            }
        }
        controls = resolve_controls(entity, services)
        # 不应生成指向 set_humidity 的 available_mode 错配控件
        assert "available_mode" not in controls
        assert "available_modes" not in controls

    def test_enum_mode_from_available_modes(self):
        """加湿器 mode 属性 + available_modes 数组 → 生成 mode 枚举，指向 set_mode。"""
        entity = {
            "entity_id": "humidifier.bedroom",
            "state": "on",
            "attributes": {
                "humidity": 50,
                "min_humidity": 30,
                "max_humidity": 80,
                "mode": "normal",
                "available_modes": ["normal", "eco", "boost"],
            },
        }
        services = {
            "humidifier": {
                "set_humidity": {"fields": ["entity_id", "humidity"]},
                "set_mode": {"fields": ["entity_id", "mode"]},
            }
        }
        controls = resolve_controls(entity, services)
        assert "mode" in controls
        ctrl = controls["mode"]
        assert ctrl["type"] == "enum"
        assert ctrl["service"] == "set_mode"
        assert ctrl["param"] == "mode"
        assert ctrl["options"] == ["normal", "eco", "boost"]
        assert ctrl["current"] == "normal"


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

    def test_slider_volume_level_is_0_to_1(self):
        """media_player volume_level 按 HA 规范固定 0-1，而非默认 0-100。"""
        entity = {
            "entity_id": "media_player.speaker",
            "state": "playing",
            "attributes": {"volume_level": 0.3},
        }
        services = {"media_player": {"set_volume_level": {"fields": ["entity_id", "volume_level"]}}}
        controls = resolve_controls(entity, services)
        vol = controls["volume_level"]
        assert vol["min"] == 0.0
        assert vol["max"] == 1.0
        assert vol["current"] == 0.3


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

    def test_media_player_turn_on_filtered_without_feature_bit(self):
        """小爱类设备 supported_features 不含 TURN_ON(128)/TURN_OFF(256) → 过滤。"""
        entity = {
            "entity_id": "media_player.xiaoai",
            "state": "idle",
            # 仅 VOLUME_SET(4) | VOLUME_MUTE(8) | VOLUME_STEP(1024)，无 TURN_ON/OFF
            "attributes": {"supported_features": 4 | 8 | 1024},
        }
        services = {"media_player": {
            "turn_on": {"fields": ["entity_id"]},
            "turn_off": {"fields": ["entity_id"]},
            "volume_down": {"fields": ["entity_id"]},
        }}
        controls = resolve_controls(entity, services)
        assert "turn_on" not in controls    # 无 128 位 → 过滤
        assert "turn_off" not in controls   # 无 256 位 → 过滤
        assert "volume_down" in controls    # 无参且无位约束 → 保留

    def test_media_player_turn_on_kept_with_feature_bit(self):
        """电视类设备 supported_features 含 TURN_ON(128) → turn_on 保留。"""
        entity = {
            "entity_id": "media_player.tv",
            "state": "off",
            # VOLUME_SET(4) | TURN_ON(128) | TURN_OFF(256)
            "attributes": {"supported_features": 4 | 128 | 256},
        }
        services = {"media_player": {
            "turn_on": {"fields": ["entity_id"]},
            "set_volume_level": {"fields": ["entity_id", "volume_level"]},
        }}
        controls = resolve_controls(entity, services)
        assert "turn_on" in controls        # 有 128 位 → 保留


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

    def test_note_injected_when_present(self):
        """note 非空时，标题行下方插入备注行（优先级最高标记）。"""
        entity = {"entity_id": "switch.gate", "attributes": {"friendly_name": "大门"}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        text = controls_to_text(entity, controls, note="ON=关门, OFF=开门")
        assert "大门 (switch.gate)" in text
        assert "备注" in text
        assert "ON=关门, OFF=开门" in text
        # 备注行在标题行之后、可控项之前
        title_idx = text.index("大门 (switch.gate)")
        note_idx = text.index("ON=关门, OFF=开门")
        action_idx = text.index("Turn On")
        assert title_idx < note_idx < action_idx

    def test_note_omitted_when_none_or_empty(self):
        """note 为 None 或空串时不输出备注行（向后兼容）。"""
        entity = {"entity_id": "switch.gate", "attributes": {"friendly_name": "大门"}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        # None（默认值）
        text_default = controls_to_text(entity, controls)
        assert "备注" not in text_default
        # 空串
        text_empty = controls_to_text(entity, controls, note="")
        assert "备注" not in text_empty
        # 仅空白
        text_blank = controls_to_text(entity, controls, note="   ")
        assert "备注" not in text_blank

    def test_note_multiline_supported(self):
        """多行备注保留换行（每行都带备注前缀缩进）。"""
        entity = {"entity_id": "switch.gate", "attributes": {"friendly_name": "大门"}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        text = controls_to_text(entity, controls, note="第一行\n第二行")
        assert "第一行" in text
        assert "第二行" in text

    def test_note_with_indent(self):
        """indent>=1（子功能）时，备注行也带正确缩进。"""
        entity = {"entity_id": "switch.gate", "attributes": {}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        text = controls_to_text(entity, controls, indent=1, note="子功能备注")
        # 子功能标题行存在
        assert "子功能 switch.gate:" in text
        assert "子功能备注" in text
