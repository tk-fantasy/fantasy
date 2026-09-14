"""Tests for RuleService pure methods."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# ---------------------------------------------------------------------------
# _resolve_revised_camera — 修改规则时 camera_id 的归属判定
#
# 此前 revise_rule 的 current_brief 白名单漏了 camera_id、且 setdefault 把它钉回
# 原值，导致用户说「改绑到门口摄像头」时 LLM 根本看不到这个字段，change_summary
# 照样回一句"已绑定"—— 静默 no-op。现在允许改，但幻觉 id 必须挡掉。
# ---------------------------------------------------------------------------

class TestResolveRevisedCamera:
    def test_llm_omits_camera_keeps_current(self):
        assert RuleService._resolve_revised_camera({"type": "vision"}, "cam_1") == "cam_1"

    def test_llm_empty_camera_keeps_current(self):
        parsed = {"type": "vision", "camera_id": "   "}
        assert RuleService._resolve_revised_camera(parsed, "cam_1") == "cam_1"

    def test_known_new_camera_adopted(self):
        parsed = {"type": "vision", "camera_id": "cam_2"}
        with patch("app.services.rule_service._is_known_camera", return_value=True):
            assert RuleService._resolve_revised_camera(parsed, "cam_1") == "cam_2"

    def test_hallucinated_camera_reset_to_current(self):
        """幻觉 id 会让规则绑到不存在的那一路 → automation_service 永不评估它。"""
        parsed = {"type": "vision", "camera_id": "cam_编出来的"}
        with patch("app.services.rule_service._is_known_camera", return_value=False):
            assert RuleService._resolve_revised_camera(parsed, "cam_1") == "cam_1"

    def test_same_camera_not_revalidated(self):
        """没变化就不必查列表（省一次 camera_manager 调用）。"""
        parsed = {"type": "vision", "camera_id": "cam_1"}
        with patch("app.services.rule_service._is_known_camera") as known:
            assert RuleService._resolve_revised_camera(parsed, "cam_1") == "cam_1"
        known.assert_not_called()

    def test_type_to_weather_clears_camera(self):
        """camera_id 对非视觉规则没有意义，留着会被 ruleMismatch 标 orange。"""
        parsed = {"type": "weather", "camera_id": "cam_2"}
        with patch("app.services.rule_service._is_known_camera", return_value=True):
            assert RuleService._resolve_revised_camera(parsed, "cam_1") == ""

    def test_type_to_time_clears_camera(self):
        assert RuleService._resolve_revised_camera({"type": "time", "camera_id": "cam_1"},
                                                   "cam_1") == ""

    def test_unbound_vision_stays_unbound(self):
        """原本就没绑、LLM 也没给 → 保持空，由上层（confirm 强校验）拦住。"""
        assert RuleService._resolve_revised_camera({"type": "vision"}, "") == ""


class TestIsKnownCamera:
    def test_fails_open_without_container(self):
        """容器不可用（异构装配/单测）时放行，与 find_missing_entities 同口径。"""
        from app.services.rule_service import _is_known_camera

        with patch("app.container.get_container", side_effect=RuntimeError("no container")):
            assert _is_known_camera("cam_whatever") is True

    def test_fails_open_when_manager_missing(self):
        from app.services.rule_service import _is_known_camera

        with patch("app.container.get_container", return_value=MagicMock(camera_manager=None)):
            assert _is_known_camera("cam_whatever") is True

    def test_fails_open_on_empty_list(self):
        from app.services.rule_service import _is_known_camera

        manager = MagicMock(list_cameras=MagicMock(return_value=[]))
        with patch("app.container.get_container", return_value=MagicMock(camera_manager=manager)):
            assert _is_known_camera("cam_whatever") is True

    def test_rejects_id_outside_real_list(self):
        from app.services.rule_service import _is_known_camera

        manager = MagicMock(list_cameras=MagicMock(
            return_value=[{"id": "cam_1", "name": "研发部"}]))
        with patch("app.container.get_container", return_value=MagicMock(camera_manager=manager)):
            assert _is_known_camera("cam_1") is True
            assert _is_known_camera("cam_2") is False


# ---------------------------------------------------------------------------
# build_rule per-user 化：按 user_id 解析 chat key，无配置回退全局
# ---------------------------------------------------------------------------

class TestRuleServicePerUser:
    """build_rule per-user 化：与 scheduler_service._resolve_reminder_client 同一模式。"""

    def _make_svc(self, global_enabled=True):
        """构造带 mock 全局 client 的 RuleService。"""
        global_client = MagicMock()
        global_client.enabled = global_enabled
        global_client.chat = AsyncMock(return_value='{"condition":"晚上","actions":[],"name":"r"}')
        svc = RuleService(client=global_client)
        return svc, global_client

    @pytest.mark.asyncio
    async def test_build_rule_uses_per_user_chat_key(self):
        """有 user_id 且用户有 per-user chat key → 构造 per-user LlmChatClient，全局 client 不被调。"""
        svc, global_client = self._make_svc()

        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.clients.client_factory.LlmChatClient") as MockClient:
                mock_instance = MagicMock()
                mock_instance.chat = AsyncMock(return_value='{"condition":"晚上","actions":[],"name":"r"}')
                MockClient.return_value = mock_instance

                await svc.build_rule("晚上开灯", user_id="u-per-user")

        # per-user client 被构造（role=chat）
        MockClient.assert_called_with(role="chat")
        # per-user client 的 chat 被调，全局 client 不该被调
        mock_instance.chat.assert_awaited()
        global_client.chat.assert_not_awaited()
        # per-user key 覆盖了私有字段
        assert mock_instance._api_key == "per-user-secret"
        assert mock_instance._base_url == "https://per-user.example.com/v1"
        assert mock_instance._model == "per-user-model"
        assert mock_instance._enabled is True  # 关键坑：_enabled 必须覆盖

    @pytest.mark.asyncio
    async def test_build_rule_falls_back_to_global_when_no_per_user_key(self):
        """有 user_id 但用户无 per-user chat key → 回退全局 self._client。"""
        svc, global_client = self._make_svc()

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            result = await svc.build_rule("晚上开灯", user_id="u-no-config")

        global_client.chat.assert_awaited()
        assert "condition" in result

    @pytest.mark.asyncio
    async def test_build_rule_no_user_id_uses_global(self):
        """无 user_id（老调用）→ 直接走全局 self._client，resolve 不被调。"""
        svc, global_client = self._make_svc()

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)) as mock_resolve:
            await svc.build_rule("晚上开灯")

        mock_resolve.assert_not_awaited()
        global_client.chat.assert_awaited()

    @pytest.mark.asyncio
    async def test_build_rule_per_user_key_overrides_disabled_global(self):
        """全局 client.enabled=False，但有 per-user key → 仍走 per-user client。

        验证"先 resolve 再检查 enabled"的坑：build_rule 第一行不再硬看全局 enabled，
        而是先解析 per-user client（_enabled=True），绕过全局占位符禁用态。
        """
        svc, global_client = self._make_svc(global_enabled=False)

        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.clients.client_factory.LlmChatClient") as MockClient:
                mock_instance = MagicMock()
                mock_instance.chat = AsyncMock(return_value='{"condition":"晚上","actions":[],"name":"r"}')
                MockClient.return_value = mock_instance

                result = await svc.build_rule("晚上开灯", user_id="u-per-user")

        # per-user client 被调（绕过了全局 disabled 的早返回）
        mock_instance.chat.assert_awaited()
        # 不会走 fallback（fallback 的 condition 是空字符串）
        assert result.get("condition") == "晚上"


class TestRuleServiceNotes:
    """规则生成注入用户自定义备注（Task 3）。"""

    @pytest.mark.asyncio
    async def test_build_rule_injects_note_into_prompt(self):
        """rule_service 把 entity_note 备注拼进 system prompt，LLM 据此正确选 service。"""
        # Mock Database.get().prefs_get_by_scope 返回备注
        mock_db = MagicMock()
        mock_db.prefs_get_by_scope = AsyncMock(return_value={
            "switch.gate": "ON=关门, OFF=开门。用户说开门时调 turn_off",
        })

        # mock client：截获 chat 调用，断言 prompt 含备注
        mock_client = MagicMock()
        mock_client.enabled = True
        captured = {}

        async def fake_chat(messages, max_tokens=None, **kw):
            captured["prompt"] = messages[0]["content"] if messages else ""
            return '{"name":"r","type":"vision","condition":"有人","actions":[],"action_descriptions":[],"cooldown_seconds":10,"summary":""}'

        mock_client.chat = AsyncMock(side_effect=fake_chat)
        svc = RuleService(client=mock_client)

        async def _devices():
            return [
                {"entity_id": "switch.gate", "state": "off", "domain": "switch",
                 "name": "大门", "attributes": {"friendly_name": "大门"}},
            ]

        async def _services():
            return {"switch": {"turn_on": ["entity_id"], "turn_off": ["entity_id"]}}

        # 注入 devices provider（含 attributes 的完整设备）
        svc.set_ha_devices_provider(_devices)
        svc.set_ha_services_provider(_services)
        svc.set_ha_catalog_provider(lambda: "- switch.gate (类型:switch, 状态:off) 名称:大门")

        with patch("app.core.database.Database.get", return_value=mock_db), \
             patch("app.core.key_resolver.resolve_key_for_role_user", new=AsyncMock(return_value=None)):
            await svc.build_rule("大门", user_id="u1")

        assert "ON=关门, OFF=开门" in captured["prompt"]
        assert "备注" in captured["prompt"]



# ---------------------------------------------------------------------------
# 幻觉设备的确定性自动匹配：重试耗尽后代码层强制替换为最接近的真实设备
# ---------------------------------------------------------------------------

class TestAutoRepairActions:
    """_auto_repair_actions：用户说的设备不存在时强制匹配近似真实设备，弹窗里二次核对。"""

    DEVICES = [
        {"entity_id": "switch.da_men_kai_guan", "name": "大门开关", "domain": "switch"},
        {"entity_id": "light.ke_ting", "name": "客厅灯", "domain": "light"},
        {"entity_id": "sensor.men_ci", "name": "门磁传感器", "domain": "sensor"},
    ]
    SERVICES = {"switch": {"turn_on": [], "turn_off": []},
                "light": {"turn_on": [], "turn_off": []}}

    def _svc(self):
        client = MagicMock()
        client.enabled = True
        return RuleService(client=client)

    @staticmethod
    def _hallucinated(description: str) -> dict:
        return {"actions": [{"mcp_tool_name": "ha_devices___call_service",
                             "mcp_tool_input": {"domain": "cover", "service": "open_cover",
                                                "entity_id": "cover.front_door", "data": {}}}],
                "action_descriptions": [description]}

    def test_repairs_hallucinated_entity_by_description(self):
        """「打开大门」+ 幻觉 cover.front_door → 替换为大门开关，domain/service 同步修正。"""
        svc = self._svc()
        parsed = self._hallucinated("打开大门")

        corrections = svc._auto_repair_actions(parsed, self.DEVICES, self.SERVICES)

        assert len(corrections) == 1
        assert corrections[0]["from"] == "cover.front_door"
        assert corrections[0]["to_name"] == "大门开关"
        ti = parsed["actions"][0]["mcp_tool_input"]
        assert ti["entity_id"] == "switch.da_men_kai_guan"
        assert ti["domain"] == "switch"
        assert ti["service"] == "turn_on"  # open 意图 → switch 的 turn_on
        assert ti["data"] == {}  # 幻觉设备的 data 不适用于新设备
        assert parsed["auto_corrections"] == corrections

    def test_close_intent_maps_to_turn_off(self):
        svc = self._svc()
        parsed = self._hallucinated("关闭大门")
        parsed["actions"][0]["mcp_tool_input"]["service"] = "close_cover"

        svc._auto_repair_actions(parsed, self.DEVICES, self.SERVICES)

        assert parsed["actions"][0]["mcp_tool_input"]["service"] == "turn_off"

    def test_cover_domain_keeps_open_cover(self):
        """家里真有窗帘机时替换到 cover，service 保留 open_cover 而不是降级 turn_on。"""
        svc = self._svc()
        devices = self.DEVICES + [{"entity_id": "cover.yang_tai", "name": "阳台窗帘",
                                   "domain": "cover"}]
        services = dict(self.SERVICES, cover={"open_cover": [], "close_cover": []})
        parsed = self._hallucinated("打开阳台窗帘")

        svc._auto_repair_actions(parsed, devices, services)

        ti = parsed["actions"][0]["mcp_tool_input"]
        assert ti["entity_id"] == "cover.yang_tai"
        assert ti["service"] == "open_cover"

    def test_zero_match_leaves_action_untouched(self):
        """完全无近似设备（只有不可控 sensor 命中）→ 不硬塞，留给调用方拦截。"""
        svc = self._svc()
        parsed = self._hallucinated("打开门磁")

        corrections = svc._auto_repair_actions(parsed, self.DEVICES, self.SERVICES)

        assert corrections == []
        assert "auto_corrections" not in parsed
        assert parsed["actions"][0]["mcp_tool_input"]["entity_id"] == "cover.front_door"

    def test_valid_entity_untouched(self):
        svc = self._svc()
        parsed = {"actions": [{"mcp_tool_name": "ha_devices___call_service",
                               "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                                  "entity_id": "light.ke_ting", "data": {}}}],
                  "action_descriptions": ["开客厅灯"]}

        assert svc._auto_repair_actions(parsed, self.DEVICES, self.SERVICES) == []

    def test_friendly_hints_carry_names_for_retry(self):
        """重试反馈带友好名候选——此前只贴拼音 id 清单，模型对不上"门"。"""
        svc = self._svc()
        parsed = self._hallucinated("打开大门")

        hints = svc._friendly_device_hints(parsed, self.DEVICES)

        assert "大门开关" in hints
        assert "switch.da_men_kai_guan" in hints


class TestBuildRuleAutoRepair:
    """build_rule 重试耗尽后的自动修复集成：修好带 auto_corrections，修不好挂 validation_errors。"""

    CATALOG = (
        "- switch.da_men_kai_guan (类型:switch, 状态:off) 名称:大门开关\n"
        "- light.ke_ting (类型:light, 状态:off) 名称:客厅灯\n"
        "- sensor.men_ci (类型:sensor, 状态:12) 名称:门磁传感器\n"
    )

    def _hallucinated(self, description: str) -> str:
        import json as _json
        return _json.dumps({
            "name": description[:10], "condition": "有人", "type": "vision",
            "actions": [{"mcp_tool_name": "ha_devices___call_service",
                         "mcp_tool_input": {"domain": "cover", "service": "open_cover",
                                            "entity_id": "cover.front_door", "data": {}}}],
            "action_descriptions": [description], "summary": f"有人就{description}",
        }, ensure_ascii=False)

    def _svc(self, description: str):
        client = MagicMock()
        client.enabled = True
        client.chat = AsyncMock(return_value=self._hallucinated(description))
        svc = RuleService(client=client)
        svc.set_ha_catalog_provider(lambda: self.CATALOG)
        svc.set_ha_devices_provider(AsyncMock(return_value=TestAutoRepairActions.DEVICES))
        svc.set_ha_services_provider(AsyncMock(return_value=TestAutoRepairActions.SERVICES))
        return svc, client

    @pytest.mark.asyncio
    async def test_exhausted_retries_auto_repair(self):
        """LLM 3 轮都幻觉同一设备 → 代码层替换为大门开关，auto_corrections 可核对。"""
        svc, client = self._svc("打开大门")

        result = await svc.build_rule("创建规则：有人就打开大门", camera_id="")

        assert client.chat.await_count == 3  # 首次 + MAX_RETRIES 轮重试
        assert "validation_errors" not in result
        assert result["actions"][0]["mcp_tool_input"]["entity_id"] == "switch.da_men_kai_guan"
        assert result["auto_corrections"][0]["to_name"] == "大门开关"

    @pytest.mark.asyncio
    async def test_zero_match_flags_validation_errors(self):
        """说的设备家里根本没有（连近似都没有）→ 挂 validation_errors 由调用方拦截。"""
        svc, _ = self._svc("打开窗帘")  # 模拟器没有窗帘机，"窗帘"零命中

        result = await svc.build_rule("创建规则：有人就打开窗帘", camera_id="")

        assert "validation_errors" in result
        assert any("cover.front_door" in e for e in result["validation_errors"])


class TestExplainEntityMapping:
    """explain：附实体对照表——乱码 entity_id 靠"拼音翻译"认不出设备。"""

    @pytest.mark.asyncio
    async def test_explain_injects_entity_mapping_and_camera(self):
        client = MagicMock()
        client.enabled = True
        client.chat = AsyncMock(return_value="控制大门开关")
        svc = RuleService(client=client)
        svc.set_ha_devices_provider(AsyncMock(return_value=TestAutoRepairActions.DEVICES))
        rule = {"actions": [{"mcp_tool_name": "ha_devices___call_service",
                             "mcp_tool_input": {"domain": "switch", "service": "turn_on",
                                                "entity_id": "switch.da_men_kai_guan", "data": {}}}],
                "camera_id": "cam_1"}

        await svc.explain_rule(rule, "控制的 id 是哪个")

        user_msg = client.chat.await_args.args[0][1]["content"]
        assert "实体对照" in user_msg
        assert "switch.da_men_kai_guan → 大门开关" in user_msg
        assert "cam_1" in user_msg  # camera_id 进 brief，"看的哪个摄像头"答得出
