"""Tests for ValidatorAgent retry logic."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.validator_agent import ValidatorAgent


def _mock_llm(response_content: str = '{"need_retry": false}') -> MagicMock:
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = response_content
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    return mock_llm


class _StubHAService:
    """get_states_snapshot 桩：断言核查读真实状态用。"""

    def __init__(self, states: list[dict]):
        self._states = states
        self.snapshot_calls = 0

    async def get_states_snapshot(self) -> list[dict]:
        self.snapshot_calls += 1
        return self._states


class TestShouldRetry:
    @pytest.mark.asyncio
    async def test_empty_content_no_retry(self):
        validator = ValidatorAgent(max_retries=1)
        result = await validator.should_retry("", 0)
        assert result is False

    @pytest.mark.asyncio
    async def test_whitespace_content_no_retry(self):
        validator = ValidatorAgent(max_retries=1)
        result = await validator.should_retry("   ", 0)
        assert result is False

    @pytest.mark.asyncio
    async def test_llm_returns_true(self):
        """无行动性表态 + query 带控制意图 → 走 LLM 语义兜底，判 true 重试。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": true}')
        validator._llm = mock_llm

        result = await validator.should_retry("收到，我马上处理", 0, query="把客厅灯关了")
        assert result is True
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_returns_false(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_llm

        result = await validator.should_retry("收到，我明白了", 0, query="把客厅灯关了")
        assert result is False
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_chitchat_query_skips_llm(self):
        """纯闲聊 query（你好）+ 无行动性表态 → 零 LLM 调用直接通过。

        旧版这里每轮闲聊都白付一次校验 LLM 调用，新流水线按 query 意图闸门跳过。
        """
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": true}')  # 就算 LLM 会判 true 也不该被调
        validator._llm = mock_llm

        result = await validator.should_retry("你好！有什么可以帮你？", 0, query="你好")
        assert result is False
        mock_llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_exception_returns_false(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("API error"))
        validator._llm = mock_llm

        result = await validator.should_retry("收到，我明白了", 0, query="把客厅灯关了")
        assert result is False

    @pytest.mark.asyncio
    async def test_content_truncated_to_500(self):
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm("false")
        validator._llm = mock_llm

        long_content = "x" * 1000
        await validator.should_retry(long_content, 0, query="把客厅灯关了")

        call_args = mock_llm.ainvoke.call_args
        messages = call_args[0][0]
        assert len(messages[1].content) <= 500


class TestBuildRetryMessage:
    def test_returns_human_message(self):
        validator = ValidatorAgent(max_retries=1)
        message = validator.build_retry_message()
        assert isinstance(message, HumanMessage)

    def test_contains_tool_call_instruction(self):
        validator = ValidatorAgent(max_retries=1)
        message = validator.build_retry_message()
        assert "tool_call" in message.content

    def test_content_not_empty(self):
        validator = ValidatorAgent(max_retries=1)
        message = validator.build_retry_message()
        assert len(message.content) > 0

    def test_pending_directive_message_preferred(self):
        """should_retry 留下的定向消息（状态不符/幻觉）优先于通用消息。"""
        validator = ValidatorAgent(max_retries=1)
        validator._pending_retry_message = HumanMessage(content="定向纠正消息")
        message = validator.build_retry_message()
        assert message.content == "定向纠正消息"
        # 取走即清：下次回退通用消息
        assert "tool_call" in validator.build_retry_message().content


class TestValidatorInit:
    def test_default_max_retries(self):
        validator = ValidatorAgent()
        assert validator._max_retries == 1

    def test_custom_max_retries(self):
        validator = ValidatorAgent(max_retries=3)
        assert validator._max_retries == 3

    def test_initial_llm_is_none(self):
        validator = ValidatorAgent()
        assert validator._llm is None


# ---------------------------------------------------------------------------
# per-user 化：should_retry 按 user_id 解析 chat key，无配置回退全局
# ---------------------------------------------------------------------------

class TestValidatorPerUser:
    """ValidatorAgent per-user：主聊天重试时 validator 与主对话用同一模型。"""

    @pytest.mark.asyncio
    async def test_should_retry_uses_per_user_llm_when_configured(self):
        """有 user_id 且用户有 per-user chat key → 构造 per-user LLM，ainvoke 走 per-user 实例。"""
        validator = ValidatorAgent(max_retries=1)
        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }
        mock_llm = _mock_llm('{"need_retry": true}')

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)):
            with patch("app.agents.validator_agent.ChatOpenAI", return_value=mock_llm) as MockChat:
                with patch("app.clients.http_client.new_client"), \
                     patch("app.clients.http_client.new_sync_client"):
                    result = await validator.should_retry(
                        "收到，我马上处理", 0, user_id="u1", query="把客厅灯关了")

        assert result is True
        # per-user LLM 被构造（带 per-user key）
        MockChat.assert_called_once()
        _, kwargs = MockChat.call_args
        assert kwargs["api_key"] == "per-user-secret"
        assert kwargs["model"] == "per-user-model"
        # ainvoke 走 per-user 实例
        mock_llm.ainvoke.assert_awaited_once()
        # per-user LLM 被缓存
        assert "u1" in validator._user_llms

    @pytest.mark.asyncio
    async def test_should_retry_falls_back_to_global_when_no_per_user_key(self):
        """有 user_id 但用户无 per-user chat key → 回退全局 _llm。"""
        validator = ValidatorAgent(max_retries=1)
        mock_global_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_global_llm

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            result = await validator.should_retry(
                "收到，我明白了", 0, user_id="u-no-config", query="把客厅灯关了")

        assert result is False
        # 全局 LLM 的 ainvoke 被调
        mock_global_llm.ainvoke.assert_awaited_once()
        # 无 per-user 配置不缓存
        assert "u-no-config" not in validator._user_llms

    @pytest.mark.asyncio
    async def test_should_retry_no_user_id_uses_global(self):
        """无 user_id → 直接走全局 _llm，不调 key 解析。"""
        validator = ValidatorAgent(max_retries=1)
        mock_global_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_global_llm

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value={"api_key": "should-not-be-called"})) as mock_resolve:
            result = await validator.should_retry(
                "收到，我明白了", 0, user_id="", query="把客厅灯关了")

        assert result is False
        # 无 user_id 不调 key 解析
        mock_resolve.assert_not_awaited()
        mock_global_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_per_user_llm_cached_across_calls(self):
        """同一 user_id 多次调 should_retry 只解析一次 key，复用缓存的 LLM。"""
        validator = ValidatorAgent(max_retries=1)
        per_user_key = {
            "api_key": "per-user-secret",
            "base_url": "https://per-user.example.com/v1",
            "model": "per-user-model",
        }
        mock_llm = _mock_llm('{"need_retry": false}')

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=per_user_key)) as mock_resolve:
            with patch("app.agents.validator_agent.ChatOpenAI", return_value=mock_llm) as MockChat:
                with patch("app.clients.http_client.new_client"), \
                     patch("app.clients.http_client.new_sync_client"):
                    await validator.should_retry(
                        "收到，我马上处理", 0, user_id="u1", query="把客厅灯关了")
                    await validator.should_retry(
                        "明白，我记下了", 0, user_id="u1", query="把客厅灯关了")

        # key 解析只调一次（第二次命中缓存）
        assert mock_resolve.await_count == 1
        # ChatOpenAI 只构造一次
        assert MockChat.call_count == 1
        # ainvoke 调了两次（复用同一实例）
        assert mock_llm.ainvoke.await_count == 2


# ---------------------------------------------------------------------------
# 硬性规则（旧路径，claim_verify_enabled=False 时生效）
# ---------------------------------------------------------------------------

class TestHardRule:
    """validator_agent 硬性规则：声称已完成控制操作但没调工具 → 强制重试。

    _ACTION_DONE_RE 只匹配"已+控制动词"（打开/关闭/调节/设置/切换），
    不匹配"完成/搞定/好了"等通用词——后者在闲聊里太常见会误判。
    """

    def _legacy_config(self):
        """关闭断言核查流水线，回退旧行为。"""
        return patch(
            "app.agents.validator_agent.get_config",
            side_effect=lambda k, d=None: False if k == "chat_assistant.claim_verify_enabled" else d,
        )

    @pytest.mark.asyncio
    async def test_claims_control_done_without_tool_calls_forces_retry(self):
        """（旧路径）说"已打开"但没调工具 → 强制重试，零 LLM 调用。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock()
        validator._llm = mock_llm

        with self._legacy_config():
            result = await validator.should_retry("已经帮你打开了", 0)
        assert result is True
        # 硬规则直接返回，不浪费一次 LLM 调用
        mock_llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claims_done_with_tool_calls_goes_through_llm(self):
        """有真实工具调用（tool_calls>0）→ 不触发硬规则，走 LLM 语义判断。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_llm

        with self._legacy_config():
            result = await validator.should_retry("已经帮你打开了", 2)
        assert result is False
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generic_done_words_not_trigger_hard_rule(self):
        """通用完成词（完成/搞定/好了）不触发表态正则 → 闲聊静默通过。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_llm

        for content in ["计划好了，稍后执行", "方案完成了", "这事搞定了"]:
            result = await validator.should_retry(content, 0, query="你好")
            assert result is False, f"通用词误触发: {content}"
        mock_llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_action_claim_goes_through_llm(self):
        """无行动性表态 + query 带控制意图 → 走 LLM 语义兜底。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_llm

        result = await validator.should_retry("好的，我知道了", 0, query="客厅灯开着吗")
        assert result is False
        mock_llm.ainvoke.assert_awaited_once()


# ---------------------------------------------------------------------------
# 断言核查流水线（新默认路径）：三时态表态 → 实体匹配 → 状态核对
# ---------------------------------------------------------------------------

class TestClaimVerification:
    """行动性表态的代码级核查：零 LLM 调用，用 HA 真实状态裁决。"""

    @pytest.mark.asyncio
    async def test_chitchat_claim_silent_pass(self):
        """闲聊里的"已经设置好了"（无设备名词、query 无意图）→ 静默通过。

        旧硬规则会把"已设置"盲目重试（误伤），新流水线按实体核对过滤。
        """
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": true}')
        validator._llm = mock_llm

        result = await validator.should_retry("方案已经设置好了", 0, query="你好")
        assert result is False
        mock_llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claim_no_entity_but_query_intent_retries(self):
        """表态有动词但提取不到设备指称 + query 带控制意图 → 重试（零 LLM）。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": true}')
        validator._llm = mock_llm

        result = await validator.should_retry("已经帮你打开了", 0, query="把灯打开")
        assert result is True
        mock_llm.ainvoke.assert_not_awaited()
        message = validator.build_retry_message()
        assert "get_entities" in message.content

    @pytest.mark.asyncio
    async def test_claim_no_entity_no_intent_silent(self):
        """表态无设备指称、query 也无控制意图（病态闲聊）→ 静默。"""
        validator = ValidatorAgent(max_retries=1)
        validator._llm = _mock_llm('{"need_retry": true}')

        result = await validator.should_retry("已经帮你打开了", 0, query="你好")
        assert result is False

    @pytest.mark.asyncio
    async def test_state_mismatch_directed_retry(self):
        """说"已打开"但 HA 实际是 off → 定向重试，消息注入真实状态。"""
        ha = _StubHAService([{"entity_id": "light.kt", "state": "off", "attributes": {}}])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)
        validator._llm = _mock_llm('{"need_retry": false}')

        result = await validator.should_retry(
            "已经打开客厅灯了", 0, query="把客厅灯打开",
            entity_name_map={"light.kt": "客厅灯"})
        assert result is True
        # 零 LLM 调用
        validator._llm.ainvoke.assert_not_awaited()
        message = validator.build_retry_message()
        assert "light.kt" in message.content
        assert "关闭(off)" in message.content

    @pytest.mark.asyncio
    async def test_state_match_silent_pass(self):
        """说"已打开"且 HA 实际就是 on → 静默通过，不再瞎重试。"""
        ha = _StubHAService([{"entity_id": "light.kt", "state": "on", "attributes": {}}])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)

        result = await validator.should_retry(
            "已经打开客厅灯了", 0, query="把客厅灯打开",
            entity_name_map={"light.kt": "客厅灯"})
        assert result is False
        assert ha.snapshot_calls == 1

    @pytest.mark.asyncio
    async def test_entity_before_verb_word_order(self):
        """「客厅灯已经打开了」——宾语在动词前的语序也能提取。"""
        ha = _StubHAService([{"entity_id": "light.kt", "state": "off", "attributes": {}}])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)

        result = await validator.should_retry(
            "客厅灯已经打开了", 0, query="把客厅灯打开",
            entity_name_map={"light.kt": "客厅灯"})
        assert result is True

    @pytest.mark.asyncio
    async def test_will_tense_direct_retry_without_state_read(self):
        """「我将帮你关闭」+ 0 次工具调用 = 确定性未执行 → 直接重试，不查状态。"""
        ha = _StubHAService([{"entity_id": "light.kt", "state": "on", "attributes": {}}])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)
        validator._llm = _mock_llm('{"need_retry": true}')

        result = await validator.should_retry(
            "我将帮你关闭客厅灯", 0, query="把客厅灯关了",
            entity_name_map={"light.kt": "客厅灯"})
        assert result is True
        # 将要时态不查状态（零 HA 读、零 LLM）
        assert ha.snapshot_calls == 0
        validator._llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hallucinated_device_retries(self):
        """说"已打开扫地机器人"但设备列表里没有 → 幻觉信号，重试查 get_entities。"""
        validator = ValidatorAgent(max_retries=1, ha_service=_StubHAService([]))
        validator._llm = _mock_llm('{"need_retry": false}')

        result = await validator.should_retry(
            "已经打开扫地机器人", 0, query="打开扫地机器人",
            entity_name_map={"light.kt": "客厅灯"})
        assert result is True
        message = validator.build_retry_message()
        assert "get_entities" in message.content

    @pytest.mark.asyncio
    async def test_inconclusive_state_generic_retry(self):
        """调温类断言（空调已调到26度）状态语义不可核 → 通用强制重试（旧行为）。"""
        ha = _StubHAService([{"entity_id": "climate.bed", "state": "cool", "attributes": {}}])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)

        result = await validator.should_retry(
            "空调已调到26度", 0, query="空调调到26度",
            entity_name_map={"climate.bed": "空调"})
        assert result is True
        # 通用重试：无定向消息
        assert validator._pending_retry_message is None

    @pytest.mark.asyncio
    async def test_cover_open_close_states(self):
        """窗帘按 cover 域核对：已拉开 + 实际 open → 静默通过。"""
        ha = _StubHAService([{"entity_id": "cover.curtain", "state": "open", "attributes": {}}])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)

        result = await validator.should_retry(
            "窗帘已经拉开了", 0, query="把窗帘拉开",
            entity_name_map={"cover.curtain": "窗帘"})
        assert result is False

    @pytest.mark.asyncio
    async def test_no_claim_chitchat_zero_calls(self):
        """无表态 + 闲聊 query → 零 LLM、零 HA 读。"""
        ha = _StubHAService([])
        validator = ValidatorAgent(max_retries=1, ha_service=ha)
        validator._llm = _mock_llm('{"need_retry": true}')

        result = await validator.should_retry("你好呀，今天天气不错", 0, query="你好")
        assert result is False
        validator._llm.ainvoke.assert_not_awaited()
        assert ha.snapshot_calls == 0

    @pytest.mark.asyncio
    async def test_no_claim_control_query_uses_llm_net(self):
        """无表态 + query 带控制意图 → LLM 语义兜底仍生效（正则漏网保险）。"""
        validator = ValidatorAgent(max_retries=1)
        mock_llm = _mock_llm('{"need_retry": false}')
        validator._llm = mock_llm

        result = await validator.should_retry("我明白了", 0, query="把灯关一下")
        assert result is False
        mock_llm.ainvoke.assert_awaited_once()


class TestParseNeedRetry:
    """_parse_need_retry：json.loads 优先，降级词边界匹配。"""

    def test_valid_json_true(self):
        assert ValidatorAgent._parse_need_retry('{"need_retry": true}') is True

    def test_valid_json_false(self):
        assert ValidatorAgent._parse_need_retry('{"need_retry": false}') is False

    def test_plain_text_true(self):
        """LLM 偶尔只返回 "true"。"""
        assert ValidatorAgent._parse_need_retry("true") is True

    def test_plain_text_false(self):
        assert ValidatorAgent._parse_need_retry("false") is False

    def test_true_story_not_mismatched(self):
        """含 "true" 子串但非纯 true 词 → 不误判为需重试。"""
        assert ValidatorAgent._parse_need_retry("true story") is False
        assert ValidatorAgent._parse_need_retry("not true at all") is False

    def test_explanatory_text_without_true(self):
        """解释性文本无 true → False。"""
        assert ValidatorAgent._parse_need_retry("模型只是闲聊，不需要重试") is False
