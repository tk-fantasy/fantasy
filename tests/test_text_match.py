"""match_devices 剥离/匹配测试。

回归点：用户说「帮我开下灯」时，请求前缀「帮我」+ 复合动词「开下」必须被剥掉，
否则 match_devices 返回空 → 可控项不注入 system prompt → LLM 编造 HA 不存在的服务
（如 light.set_level）。

另覆盖空调 hvac 模式（制热/制冷/除湿/送风）+ 「模式」后缀的剥离：未补 enum_words
前「把空调调到制热模式」匹配 0 个，补后唯一命中。
"""
from __future__ import annotations

import pytest

from app.utils.text_match import match_devices


# 模拟一套含多盏灯 / 多台空调 / 风扇 / 加湿器的真实设备
DEVICES = [
    {"entity_id": "light.bedroom_bedside", "name": "床头灯",   "area_name": "卧室", "domain": "light"},
    {"entity_id": "light.living_main",     "name": "客厅吊灯", "area_name": "客厅", "domain": "light"},
    {"entity_id": "light.living_stripe",   "name": "客厅灯带", "area_name": "客厅", "domain": "light"},
    {"entity_id": "light.study_desk",      "name": "台灯",     "area_name": "书房", "domain": "light"},
    {"entity_id": "climate.living_ac",     "name": "中央空调", "area_name": "客厅", "domain": "climate"},
    {"entity_id": "fan.living_fan",        "name": "客厅风扇", "area_name": "客厅", "domain": "fan"},
    {"entity_id": "humidifier.bedroom",    "name": "卧室加湿器", "area_name": "卧室", "domain": "humidifier"},
]


class TestMatchDevicesStripping:
    """动词 / 请求前缀剥离后应正确命中设备集合。"""

    @pytest.mark.parametrize("query,expected_names", [
        # 触发 bug 的核心场景：请求前缀 + 「开下」复合动词
        ("帮我开下灯",   ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("给我关下空调", ["中央空调"]),
        ("麻烦你把灯打开", ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("请开灯",       ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        # 「开下 / 开一下」复合动词（无前缀）
        ("开下灯",       ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("开一下灯",     ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        # 原有句式不能回归
        ("开灯",         ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("把灯关了",     ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("关空调",       ["中央空调"]),
    ])
    def test_stripping_yields_multi_match(self, query, expected_names):
        matched = match_devices(query, DEVICES)
        assert [d["name"] for d in matched] == expected_names

    @pytest.mark.parametrize("query,expected_name", [
        ("客厅吊灯", "客厅吊灯"),   # 完整设备名 → 唯一匹配
        ("中央空调", "中央空调"),
    ])
    def test_unique_match(self, query, expected_name):
        matched = match_devices(query, DEVICES)
        assert len(matched) == 1
        assert matched[0]["name"] == expected_name

    def test_alias_yields_empty(self):
        """用户用别名 / 型号（子串匹配不上）→ 空匹配。"""
        assert match_devices("把飞利浦那盏打开", DEVICES) == []

    def test_empty_inputs(self):
        assert match_devices("", DEVICES) == []
        assert match_devices("开灯", []) == []


class TestStripParameterValue:
    """调节类指令带属性词 + 数值时，剥离后应正确命中设备。

    回归点：用户「调亮度到70 灯」「帮我把灯的亮度调整到70」这类带数值/属性词
    的 query，旧逻辑（fuzzy_match 2-gram）剥不掉「亮度」「到70」「调整」→
    判定不匹配 → 可控项不注入 system prompt → LLM 编造 light.set_level。
    """

    @pytest.mark.parametrize("query,expected_names", [
        # 带「属性词+到+数值」的调节指令
        ("调亮度到70 灯",       ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("帮我把灯的亮度调整到70", ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        # 空调温度/风速
        ("把空调温度调到26",    ["中央空调"]),
        ("设温度到26 空调",     ["中央空调"]),
        ("风速调到低 空调",     ["中央空调"]),
        # 带单位（度 / %）
        ("亮度调到50% 灯",      ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
        ("温度设到26度 空调",   ["中央空调"]),
        # 小数
        ("亮度调到50.5 灯",     ["床头灯", "客厅吊灯", "客厅灯带", "台灯"]),
    ])
    def test_strip_param_value(self, query, expected_names):
        matched = match_devices(query, DEVICES)
        assert [d["name"] for d in matched] == expected_names

    @pytest.mark.parametrize("query,expected_name", [
        ("调亮度到70 客厅吊灯", "客厅吊灯"),
        ("把客厅吊灯的亮度调整到70", "客厅吊灯"),
        ("中央空调温度调到26", "中央空调"),
    ])
    def test_unique_match_with_param(self, query, expected_name):
        """带属性词+数值的指令指向唯一设备时，应唯一匹配。"""
        matched = match_devices(query, DEVICES)
        assert len(matched) == 1
        assert matched[0]["name"] == expected_name


class TestHvacModeEnum:
    """空调 hvac 模式值（制热/制冷/除湿/送风）+ 「模式」后缀剥离。

    回归点：未补 enum_words 前「把空调调到制热模式」匹配 0 个（「制热」「模式」
    都不在剥离词表里），补后唯一命中中央空调。
    """

    @pytest.mark.parametrize("query,expected_name", [
        ("把空调调到制热模式", "中央空调"),
        ("空调制冷模式",       "中央空调"),
        ("空调除湿",           "中央空调"),
        ("空调送风模式",       "中央空调"),
    ])
    def test_hvac_mode_unique(self, query, expected_name):
        matched = match_devices(query, DEVICES)
        assert len(matched) == 1
        assert matched[0]["name"] == expected_name


class TestHumidifierMatching:
    """加湿器匹配回归。

    回归点：用户「打开加湿器」在有加湿器时应唯一命中加湿器，绝不可因空调带
    除湿（dry）模式而误命中空调；去掉加湿器后应返回空（而非降级命中空调）。
    """

    # 去掉加湿器后的设备子集（模拟「移出房间 → AI 看不到」）
    DEVICES_NO_HUMIDIFIER = [d for d in DEVICES if d["domain"] != "humidifier"]

    @pytest.mark.parametrize("query,expected_name", [
        ("打开加湿器",   "卧室加湿器"),
        ("开下加湿器",   "卧室加湿器"),
        ("关闭加湿器",   "卧室加湿器"),
        ("把加湿器打开", "卧室加湿器"),
    ])
    def test_humidifier_unique(self, query, expected_name):
        matched = match_devices(query, DEVICES)
        assert len(matched) == 1
        assert matched[0]["name"] == expected_name

    @pytest.mark.parametrize("query", [
        "打开加湿器",
        "开下加湿器",
        "关闭加湿器",
        "把加湿器打开",
    ])
    def test_no_humidifier_yields_empty(self, query):
        """去掉加湿器后，相同指令应返回空，不降级命中空调。"""
        matched = match_devices(query, self.DEVICES_NO_HUMIDIFIER)
        assert matched == []
        # 显式断言：空调不应出现在误命中结果里
        assert all(d["domain"] != "climate" for d in matched)
