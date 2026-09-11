"""match_devices 剥离/匹配测试。

回归点：用户说「帮我开下灯」时，请求前缀「帮我」+ 复合动词「开下」必须被剥掉，
否则 match_devices 返回空 → 可控项不注入 system prompt → LLM 编造 HA 不存在的服务
（如 light.set_level）。

另覆盖空调 hvac 模式（制热/制冷/除湿/送风）+ 「模式」后缀的剥离：未补 enum_words
前「把空调调到制热模式」匹配 0 个，补后唯一命中。
"""
from __future__ import annotations

import pytest

from app.utils.text_match import (
    TIER_ALL_MARKER,
    TIER_AMBIGUOUS,
    TIER_CATEGORY_MISS,
    TIER_EXACT,
    TIER_NONE,
    TIER_UNIQUE,
    classify_target,
    match_devices,
)


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


# ---------------------------------------------------------------------------
# classify_target 分层判定
# ---------------------------------------------------------------------------

def _entry(eid: str, name: str, domain: str | None = None, *,
           device_name: str = "", area: str = "客厅", state: str = "off") -> dict:
    """构造 build_match_index 口径的条目（label 缺省时由实现退回 name）。"""
    return {
        "entity_id": eid,
        "name": name,
        "domain": domain or eid.split(".")[0],
        "device_id": f"dev-{device_name or name}",
        "device_name": device_name or name,
        "label": name,
        "area_name": area,
        "state": state,
    }


# 一设备多可控子实体（真实 MIoT 命名形态，口径同 tests/test_device_registry.py 的 A_LAMP_SUBS）
A_LAMP_ENTRIES = [
    _entry("switch.a_bk_onoff",   "A灯 总开关",        device_name="A灯", area="公司"),
    _entry("switch.a_first_key",  "A灯 第一键",        device_name="A灯", area="公司"),
    _entry("switch.a_on_p2",      "A灯 会客厅灯 左键", device_name="A灯", area="公司"),
    _entry("switch.a_on_p3",      "A灯 会客厅灯 右键", device_name="A灯", area="公司"),
    _entry("switch.a_second_key", "A灯 第二键",        device_name="A灯", area="公司"),
]

# 别名造成的同名多实体：两盏灯都叫「B灯」→「开B灯」应两个全开；
# 混入一盏不同名的灯，验证 exact 不会顺手多收
B_LAMP_ENTRIES = [
    _entry("light.b1", "B灯", device_name="厨房灯",   area="厨房"),
    _entry("light.b2", "B灯", device_name="客厅吊灯", area="客厅"),
    _entry("light.bedroom_bedside", "床头灯", area="卧室"),
]

# 同名跨 domain：设备「大门」下 switch + lock。classify_target 如实返回两个，
# domain 收窄由 call_service 闸门负责（避免「开大门」顺手把门锁打开）
DA_MEN_ENTRIES = [
    _entry("switch.da_men", "大门", device_name="大门"),
    _entry("lock.da_men",   "大门", device_name="大门"),
]

# 客厅多设备：area 兜底轮会把整个客厅拉进候选，靠品类尾词「灯」收窄回灯
LIVING_ROOM_ENTRIES = [
    _entry("light.living_main",  "客厅吊灯",     area="客厅"),
    _entry("fan.living_fan",     "客厅风扇",     area="客厅"),
    _entry("cover.living_curtain", "客厅窗帘",   area="客厅"),
    _entry("switch.living_plug", "客厅智能插座", area="客厅"),
    _entry("light.bedroom_bedside", "床头灯",    area="卧室"),
]

# 设备名自带全量词：剥标记后「全屋电源」变「屋电源」，必须靠双变体兜回来
POWER_ENTRIES = [_entry("switch.quan_wu", "全屋电源", device_name="全屋电源")]

# 候选上限截断用
MANY_LIGHTS = [
    _entry(f"light.l{i}", f"测试灯{i}", area="客厅") for i in range(15)
]


def _eids(result) -> list[str]:
    return [c["entity_id"] for c in result.candidates]


class TestClassifyTargetExact:
    """精确同名（设备级 / 实体级 / 别名级）→ 全执行，不问用户。"""

    def test_device_level_exact_expands_to_all_sub_entities(self):
        """「开A灯」→ 设备名下 5 个子实体全部入选。"""
        result = classify_target("开A灯", A_LAMP_ENTRIES)
        assert result.tier == TIER_EXACT
        assert len(result.candidates) == 5

    def test_sub_entity_full_label_is_exact(self):
        """说到子功能全名 → 只命中那一个。"""
        result = classify_target("开启A灯 会客厅灯 左键", A_LAMP_ENTRIES)
        assert result.tier == TIER_EXACT
        assert _eids(result) == ["switch.a_on_p2"]

    def test_same_name_multi_entity_is_exact_not_ambiguous(self):
        """两盏灯同名「B灯」→ exact 两个全收，且不收不同名的床头灯。"""
        result = classify_target("开B灯", B_LAMP_ENTRIES)
        assert result.tier == TIER_EXACT
        assert _eids(result) == ["light.b1", "light.b2"]

    def test_exact_returns_cross_domain_entries_for_gate_to_narrow(self):
        """同名跨 domain 如实返回，收窄是闸门的职责（不在这里偷偷丢）。"""
        result = classify_target("开大门", DA_MEN_ENTRIES)
        assert result.tier == TIER_EXACT
        assert set(_eids(result)) == {"switch.da_men", "lock.da_men"}

    def test_device_name_containing_all_marker_still_exact(self):
        """设备名自带「全」→ 剥标记后失配，必须靠未剥变体兜回 exact。"""
        result = classify_target("打开全屋电源", POWER_ENTRIES)
        assert result.tier == TIER_EXACT
        assert _eids(result) == ["switch.quan_wu"]

    def test_exact_wins_over_all_marker(self):
        """「成都的灯都关了」→ exact 先判，句尾的「都」不得触发全开。"""
        entries = [_entry("light.chengdu", "成都灯", device_name="成都灯")]
        result = classify_target("成都的灯都关了", entries)
        assert result.tier == TIER_EXACT
        assert _eids(result) == ["light.chengdu"]

    def test_prefix_without_separator_is_not_exact(self):
        """「客厅灯」不得当成「客厅灯带」的精确同名（无分隔符边界）。"""
        result = classify_target("开客厅灯", DEVICES)
        assert result.tier != TIER_EXACT
        # 「客厅吊灯」中间隔了「吊」，更不能被 exact 吞掉
        assert "light.living_main" not in _eids(result)


class TestClassifyTargetAllMarker:
    """「所有/全部/都/整个/全」→ 命中集全部执行，不问用户。"""

    def test_all_marker_expands_to_whole_matched_set(self):
        result = classify_target("把所有灯关掉", DEVICES)
        assert result.tier == TIER_ALL_MARKER
        assert set(_eids(result)) == {
            "light.bedroom_bedside", "light.living_main",
            "light.living_stripe", "light.study_desk",
        }

    def test_all_marker_du(self):
        """「把灯都关了」——剥标记后才能匹配上（原句「灯都」与实体名无子串关系）。"""
        result = classify_target("把灯都关了", DEVICES)
        assert result.tier == TIER_ALL_MARKER
        assert len(result.candidates) == 4

    def test_all_marker_without_candidates_falls_through_to_none(self):
        """有全量词但一个候选都没有 → none 放行，不能返回空候选的 all_marker。"""
        result = classify_target("全开", [_entry("switch.gate", "switch.gate")])
        assert result.tier == TIER_NONE
        assert result.candidates == []


class TestClassifyTargetAmbiguous:
    """模糊多命中 → 转用户选择。"""

    def test_generic_category_word_is_ambiguous(self):
        result = classify_target("开灯", DEVICES)
        assert result.tier == TIER_AMBIGUOUS
        assert len(result.candidates) == 4
        # 非灯设备不得混进候选
        assert all(c["domain"] == "light" for c in result.candidates)

    def test_candidates_capped_at_12(self):
        result = classify_target("开灯", MANY_LIGHTS)
        assert result.tier == TIER_AMBIGUOUS
        assert len(result.candidates) == 12

    def test_unique_partial_match_is_not_ambiguous(self):
        """「开加湿器」子串命中唯一实体（名字不完全相等）→ unique 直接执行。"""
        result = classify_target("打开加湿器", DEVICES)
        assert result.tier == TIER_UNIQUE
        assert _eids(result) == ["humidifier.bedroom"]

    def test_exact_full_name_is_unique_hit(self):
        result = classify_target("开客厅吊灯", DEVICES)
        assert len(result.candidates) == 1
        assert _eids(result) == ["light.living_main"]
        assert result.tier in (TIER_EXACT, TIER_UNIQUE)


class TestClassifyTargetDomainNarrowing:
    """品类尾词收窄候选 domain —— area 兜底轮会把整个区域拉进来。"""

    def test_area_fallback_narrowed_by_category_tail(self):
        """「把客厅的灯关了」→ area 轮命中客厅全部设备，尾词「灯」收窄回吊灯一个。"""
        result = classify_target("把客厅的灯关了", LIVING_ROOM_ENTRIES)
        assert result.tier == TIER_UNIQUE
        assert _eids(result) == ["light.living_main"]

    def test_narrowing_never_empties_nonempty_candidates(self):
        """「打开客厅」无品类尾词 → 退回未过滤候选，不得被过滤成空。"""
        result = classify_target("打开客厅", LIVING_ROOM_ENTRIES)
        assert result.candidates != []
        assert result.tier == TIER_AMBIGUOUS

    def test_explicit_domain_used_when_no_category_tail(self):
        """query 里推不出品类时，用调用方传入的 domain（LLM 所选实体的 domain）。"""
        entries = [
            _entry("light.living_main", "客厅吊灯", area="客厅"),
            _entry("fan.living_fan", "客厅风扇", area="客厅"),
        ]
        result = classify_target("打开客厅", entries, domain="fan")
        assert _eids(result) == ["fan.living_fan"]
        assert result.tier == TIER_UNIQUE


class TestClassifyTargetCategoryMiss:
    """说了品类词但设备不存在 → 不许瞎猜，如实说 + 给候选。"""

    def test_nonexistent_qualified_name_offers_category_candidates(self):
        """「月球的灯」→ 没有月球灯，但尾词「灯」给出真实存在的灯作候选。"""
        result = classify_target("月球的灯", DEVICES)
        assert result.tier == TIER_CATEGORY_MISS
        # 客厅灯带以「带」结尾，不共享尾词「灯」，不在候选内
        assert set(_eids(result)) == {
            "light.bedroom_bedside", "light.living_main", "light.study_desk",
        }

    def test_single_category_member_still_offered(self):
        """全屋只有一盏灯时也要给建议，不能因为候选少就退回瞎猜。"""
        result = classify_target("打开阅读灯", [_entry("light.bed", "床头灯", area="卧室")])
        assert result.tier == TIER_CATEGORY_MISS
        assert _eids(result) == ["light.bed"]


class TestClassifyTargetNone:
    """无候选且无品类尾词 → 放行（保住「太热了→开空调」这类合理推断）。"""

    def test_alias_or_model_word_yields_none(self):
        result = classify_target("把飞利浦那盏打开", DEVICES)
        assert result.tier == TIER_NONE
        assert result.candidates == []

    def test_implicit_intent_yields_none(self):
        """「太热了」不含任何设备品类词 → 放行给 LLM 推断空调。"""
        result = classify_target("太热了", DEVICES)
        assert result.tier == TIER_NONE

    def test_empty_inputs(self):
        assert classify_target("", DEVICES).tier == TIER_NONE
        assert classify_target("开灯", []).tier == TIER_NONE
        assert classify_target("开灯", []).candidates == []

    def test_normalized_query_exposed(self):
        """normalized 供闸门写日志/文案，必须是剥离后的纯设备词。"""
        assert classify_target("帮我开下灯", DEVICES).normalized == "灯"


class TestClassifyTargetCompoundSentence:
    """复合句（一句话多条指令）整句放行，交给 LLM 拆解。

    归一化是按「单个设备词」设计的：「开灯关窗帘」会被剥成「灯关窗帘」，两轮
    子串匹配全空 → 误落 category_miss 弹框，文案还会把归一化中间态念给用户听。
    """

    def test_two_verbs_in_one_sentence_is_compound(self):
        result = classify_target("开灯关窗帘", LIVING_ROOM_ENTRIES)
        assert result.tier == TIER_NONE
        assert result.candidates == []

    def test_connective_marks_compound(self):
        result = classify_target("把灯关了然后把窗帘拉上", LIVING_ROOM_ENTRIES)
        assert result.tier == TIER_NONE

    def test_single_intent_is_not_compound(self):
        """单条指令不得被豁免，否则消歧形同虚设。"""
        assert classify_target("开灯", LIVING_ROOM_ENTRIES).tier == TIER_AMBIGUOUS
        assert classify_target("把所有灯关掉", DEVICES).tier == TIER_ALL_MARKER
        assert classify_target("月球的灯", DEVICES).tier == TIER_CATEGORY_MISS

    def test_device_name_containing_diao_is_not_compound(self):
        """「空调」自带「调」字——判据只数「开/关」，不得把它误判成复合句。"""
        result = classify_target("空调开到26度", DEVICES)
        assert result.tier == TIER_UNIQUE
        assert _eids(result) == ["climate.living_ac"]

    def test_noun_kaiguan_falls_back_to_pass_through(self):
        """「打开开关」被误判成复合句 → 退回放行（旧行为），不会误弹框或误拒。"""
        entries = [_entry("switch.wall", "客厅开关", area="客厅")]
        assert classify_target("打开开关", entries).tier == TIER_NONE
