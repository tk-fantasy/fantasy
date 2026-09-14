"""Tests for ha_routes.py - HA 设备控制。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_container(**overrides):
    """构造一个 mock AppContainer，按需覆盖字段。"""
    c = MagicMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class TestHAEntitiesRoute:
    """测试 /api/ha/entities 路由。"""

    @pytest.mark.asyncio
    async def test_ha_entities(self):
        """获取实体列表。"""
        from app.routes.ha_routes import ha_entities
        from app.services.ha_service import HAService

        mock_ha_service = MagicMock()
        mock_ha_service.get_all_devices = AsyncMock(return_value=[
            {"entity_id": "light.test", "state": "on", "domain": "light"},
        ])
        mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={
            "devices": [
                {"entities": [{"entity_id": "light.test", "domain": "light"}]},
            ],
        })
        mock_ha_service.get_service_defs = AsyncMock(return_value=[])
        container = _mock_container(ha_service=mock_ha_service)

        with patch("app.services.entity_controls.resolve_controls") as mock_controls:
            mock_controls.return_value = {}
            # get_service_defs 内部调 container.ha_client.get_services
            container.ha_client.get_services = AsyncMock(return_value=[])
            result = await ha_entities(container=container)
            assert result.code == "ok"


class TestHAServicesRoute:
    """测试 /api/ha/services 路由。"""

    @pytest.mark.asyncio
    async def test_ha_services(self):
        """获取服务列表。"""
        from app.routes.ha_routes import ha_services
        from app.services.ha_service import HAService

        mock_ha_service = MagicMock(wraps=HAService)
        container = _mock_container(ha_service=mock_ha_service)
        container.ha_client.get_services = AsyncMock(return_value=[
            {"domain": "light", "services": {
                "turn_on": {"fields": {"entity_id": {"required": False}}},
                "turn_off": {"fields": {"entity_id": {"required": False}}},
            }},
        ])
        result = await ha_services(container=container)
        assert result.code == "ok"
        assert "light" in result.data


class TestHACallServiceRoute:
    """测试 /api/ha/call_service 路由。"""

    @pytest.mark.asyncio
    async def test_ha_call_service(self):
        """调用服务成功。"""
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container()
        container.ha_client.call_service = AsyncMock(return_value={"result": "ok"})

        payload = HAServiceCallRequest(
            domain="light",
            service="turn_on",
            entity_id="light.test",
            data={}
        )
        result = await ha_call_service(payload, container=container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_ha_call_service_invalidates_cache(self):
        """调用服务后应清掉 HAService 状态缓存，确保前端重拉拿到最新状态。"""
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container()
        container.ha_client.call_service = AsyncMock(return_value={"result": "ok"})
        container.ha_service.invalidate_states_cache = MagicMock()

        payload = HAServiceCallRequest(
            domain="light",
            service="turn_on",
            entity_id="light.test",
            data={}
        )
        await ha_call_service(payload, container=container)
        container.ha_service.invalidate_states_cache.assert_called_once()


class TestHAConfigRoute:
    """测试 /api/ha/config 路由。"""

    @pytest.mark.asyncio
    async def test_get_ha_config(self):
        """获取 HA 配置。"""
        from app.routes.ha_routes import get_ha_config

        with patch("app.routes.ha_routes.get_config") as mock_get_config:
            mock_get_config.return_value = {
                "url": "http://localhost:8123",
                "token": "test-token-12345678"
            }
            result = await get_ha_config()
            assert result.code == "ok"
            assert result.data["url"] == "http://localhost:8123"


class TestHAHistoryRoute:
    """测试 /api/ha/history 路由。"""

    @pytest.mark.asyncio
    async def test_ha_history_returns_data(self):
        """查询历史成功，返回 history 数组。"""
        from app.routes.ha_routes import ha_history

        container = _mock_container()
        container.ha_client.get_history = AsyncMock(return_value=[
            [{"entity_id": "sensor.temp", "state": "26.5", "last_updated": "2026-07-13T00:00:00+00:00"}],
        ])
        result = await ha_history(
            filter_entity_id="sensor.temp", hours=24, container=container,
        )
        assert result.code == "ok"
        assert result.data["count"] == 1
        assert len(result.data["history"]) == 1
        # 验证传给 client 的参数含 timestamp/end_time
        call_kwargs = container.ha_client.get_history.call_args.kwargs
        assert call_kwargs["filter_entity_id"] == "sensor.temp"
        assert call_kwargs["timestamp"]  # 非空 ISO8601
        assert call_kwargs["end_time"]
        assert call_kwargs["minimal"]  # truthy（直接调用路由时是 Query(True)，FastAPI 运行时会解包为 True）

    @pytest.mark.asyncio
    async def test_ha_history_empty_result(self):
        """无历史数据时返回空数组而非报错。"""
        from app.routes.ha_routes import ha_history

        container = _mock_container()
        container.ha_client.get_history = AsyncMock(return_value=[])
        result = await ha_history(
            filter_entity_id="sensor.nodata", hours=6, container=container,
        )
        assert result.code == "ok"
        assert result.data["count"] == 0


class TestEntityNotesRoute:
    """测试 /api/ha/entity-notes 路由（Task 5）。"""

    @pytest.fixture
    async def _db(self, tmp_path):
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        Database._write_lock = None
        with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
            await Database.init()
            yield Database.get()

    @pytest.mark.asyncio
    async def test_get_entity_notes_empty(self, _db):
        from app.routes.ha_routes import get_entity_notes
        result = await get_entity_notes()
        assert result.code == "ok"
        assert result.data == {"notes": {}}

    @pytest.mark.asyncio
    async def test_put_then_get_entity_note(self, _db):
        from app.routes.ha_routes import get_entity_notes, set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        container = _mock_container(ha_service=MagicMock())
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="ON=关门, OFF=开门"), container=container)

        result = await get_entity_notes()
        assert result.data["notes"]["switch.gate"] == "ON=关门, OFF=开门"

    @pytest.mark.asyncio
    async def test_put_empty_note_deletes(self, _db):
        from app.routes.ha_routes import get_entity_notes, set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        container = _mock_container(ha_service=MagicMock())
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="备注1"), container=container)
        # 空串删除
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note=""), container=container)

        result = await get_entity_notes()
        assert "switch.gate" not in result.data["notes"]

    @pytest.mark.asyncio
    async def test_put_triggers_catalog_refresh(self, _db):
        """写入后立即触发 catalog 刷新，让新备注进缓存（不必等后台 60 秒循环）。"""
        from app.routes.ha_routes import set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        refresh_calls = []

        async def _fake_refresh():
            refresh_calls.append(1)

        mock_ha_service = MagicMock()
        container = _mock_container(ha_service=mock_ha_service, catalog_refresh_fn=_fake_refresh)
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="x"), container=container)
        # create_task 调度，让事件循环跑一下让 task 完成
        import asyncio
        await asyncio.sleep(0.01)
        assert len(refresh_calls) == 1, f"catalog_refresh_fn 应被调一次，实际 {len(refresh_calls)} 次"

    @pytest.mark.asyncio
    async def test_put_no_refresh_fn_does_not_crash(self, _db):
        """container 没有 catalog_refresh_fn 时（旧代码/未注入）不崩溃，仅跳过刷新。"""
        from app.routes.ha_routes import set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        container = _mock_container(ha_service=MagicMock())
        # 不设 catalog_refresh_fn（getattr 默认 None）
        result = await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="x"), container=container)
        assert result.data["note"] == "x"

    @pytest.mark.asyncio
    async def test_put_missing_entity_id_rejected(self, _db):
        from app.routes.ha_routes import set_entity_note
        from app.schema.api_schemas import EntityNoteRequest
        from app.core.exceptions import AppException

        container = _mock_container(ha_service=MagicMock())
        with pytest.raises(AppException):
            await set_entity_note(EntityNoteRequest(entity_id="", note="x"), container=container)


class TestEntityOperableRoute:
    """测试 GET/PUT /ha/entity-operable 路由。"""

    @pytest.mark.asyncio
    async def test_get_entity_operable(self, tmp_path, monkeypatch):
        """GET 返回黑名单。"""
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
        await Database.init()
        await Database.get().emoji_pref_upsert("entity_operable", "lock.tong_suo", "0")
        from app.routes.ha_routes import get_entity_operable
        result = await get_entity_operable()
        assert result.code == "ok"
        assert result.data["disabled"] == {"lock.tong_suo": "0"}

    @pytest.mark.asyncio
    async def test_put_disable_then_enable(self, tmp_path, monkeypatch):
        """PUT operable=False 写黑名单，True 恢复（可逆）。"""
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
        await Database.init()
        from app.schema.api_schemas import EntityOperableRequest
        from app.routes.ha_routes import set_entity_operable
        container = _mock_container(catalog_refresh_fn=None)
        # 禁用
        await set_entity_operable(
            EntityOperableRequest(entity_id="lock.tong_suo", operable=False),
            container=container,
        )
        disabled = await Database.get().prefs_get_by_scope("entity_operable")
        assert disabled == {"lock.tong_suo": "0"}
        # 恢复
        await set_entity_operable(
            EntityOperableRequest(entity_id="lock.tong_suo", operable=True),
            container=container,
        )
        disabled = await Database.get().prefs_get_by_scope("entity_operable")
        assert disabled == {}


class TestPendingDeviceSelection:
    """POST /api/ha/pending/{id}/select|cancel —— 消歧弹框的确认路径。"""

    CANDIDATES = [
        {"entity_id": "light.a", "label": "床头灯", "domain": "light",
         "area_name": "卧室", "state": "off"},
        {"entity_id": "light.b", "label": "客厅吊灯", "domain": "light",
         "area_name": "客厅", "state": "off"},
    ]

    def _session(self, user_id="u1"):
        session = MagicMock()
        session.user_id = user_id
        session.model_messages = []
        session.pending_confirmations = {}
        return session

    def _container(self, session):
        c = _mock_container()
        c.session_store.get_session = AsyncMock(return_value=session)
        c.session_store.store_session = AsyncMock()
        c.ha_service.invalidate_states_cache = MagicMock()
        return c

    def _draft(self, session):
        from app.services.pending_selections import create_selection_draft
        return create_selection_draft(
            session, query="开灯", domain="light", service="turn_on", data={},
            candidates=self.CANDIDATES, reason="ambiguous")

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")

    @pytest.mark.asyncio
    async def test_select_executes_with_draft_action_and_appends_history(self):
        """下发的 domain/service/data 取自草稿（模型已解析好的动作），不是前端传的。"""
        from app.core.database import Database
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        session = self._session()
        pid = self._draft(session)
        container = self._container(session)
        probe = AsyncMock(return_value={"ok": 1})
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            result = await select_pending_devices(
                pid, PendingSelectRequest(session_id="s1", entity_ids=["light.b"]),
                container=container, current_user={"user_id": "u1"})
        assert result.code == "ok"
        assert result.data["entity_ids"] == ["light.b"]
        assert result.data["names"] == ["客厅吊灯"]
        assert probe.await_args.args[1:4] == ("light", "turn_on", "light.b")
        # 草稿摘除 + 合成消息 + 落盘 + 清状态缓存
        assert pid not in session.pending_confirmations
        assert session.model_messages[-1]["role"] == "user"
        assert "客厅吊灯" in session.model_messages[-1]["content"]
        container.session_store.store_session.assert_awaited_once_with(session)
        container.ha_service.invalidate_states_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_multiple_entities_joined(self):
        from app.core.database import Database
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        session = self._session()
        pid = self._draft(session)
        probe = AsyncMock(return_value={})
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            result = await select_pending_devices(
                pid, PendingSelectRequest(session_id="s1", entity_ids=["light.a", "light.b"]),
                container=self._container(session), current_user={"user_id": "u1"})
        assert probe.await_args.args[3] == "light.a,light.b"
        assert result.data["names"] == ["床头灯", "客厅吊灯"]

    # 2026-09-13 事故回归：「打开灯」的候选里混着 light.* 与墙壁开关 switch.*，
    # 草稿 domain 是模型对歧义原话猜的 light；用户勾选 switch 键后按草稿域下发
    # light/turn_on，HA 静默忽略（200 + 内部 warning），灯毫无反应。
    SWITCH_CANDIDATES = [
        {"entity_id": "switch.hkt_zuo", "label": "A灯 会客厅灯 左键", "domain": "switch",
         "area_name": "会客厅", "state": "off"},
        {"entity_id": "light.chuang_tou_deng", "label": "床头灯", "domain": "light",
         "area_name": "卧室", "state": "off"},
    ]

    @pytest.mark.asyncio
    async def test_select_switch_candidate_uses_entity_domain(self):
        """勾选的实体在别的域时，按 entity_id 前缀的域下发，不用草稿域。"""
        from app.core.database import Database
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        from app.services.pending_selections import create_selection_draft
        await Database.init()
        session = self._session()
        pid = create_selection_draft(
            session, query="打开灯", domain="light", service="turn_on", data={},
            candidates=self.SWITCH_CANDIDATES, reason="ambiguous")
        probe = AsyncMock(return_value={})
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            result = await select_pending_devices(
                pid, PendingSelectRequest(session_id="s1", entity_ids=["switch.hkt_zuo"]),
                container=self._container(session), current_user={"user_id": "u1"})
        assert probe.await_args.args[1:4] == ("switch", "turn_on", "switch.hkt_zuo")
        assert result.data["names"] == ["A灯 会客厅灯 左键"]

    @pytest.mark.asyncio
    async def test_select_mixed_domains_groups_calls_per_domain(self):
        """一次勾选跨 light+switch 时按域分组下发，每组一次调用。"""
        from app.core.database import Database
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        from app.services.pending_selections import create_selection_draft
        await Database.init()
        session = self._session()
        pid = create_selection_draft(
            session, query="打开灯", domain="light", service="turn_on", data={},
            candidates=self.SWITCH_CANDIDATES, reason="ambiguous")
        probe = AsyncMock(return_value={})
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            await select_pending_devices(
                pid, PendingSelectRequest(
                    session_id="s1", entity_ids=["switch.hkt_zuo", "light.chuang_tou_deng"]),
                container=self._container(session), current_user={"user_id": "u1"})
        assert probe.await_count == 2
        assert probe.await_args_list[0].args[1:4] == ("switch", "turn_on", "switch.hkt_zuo")
        assert probe.await_args_list[1].args[1:4] == ("light", "turn_on", "light.chuang_tou_deng")

    @pytest.mark.asyncio
    async def test_select_rejects_entity_outside_candidates(self):
        """弹框若能提交任意 entity_id，就等于开了绕过闸门和黑名单的后门。"""
        from app.core.database import Database
        from app.core.exceptions import AppException
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        session = self._session()
        pid = self._draft(session)
        probe = AsyncMock()
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            with pytest.raises(AppException) as ei:
                await select_pending_devices(
                    pid, PendingSelectRequest(session_id="s1", entity_ids=["switch.evil"]),
                    container=self._container(session), current_user={"user_id": "u1"})
        assert ei.value.http_status == 400
        probe.assert_not_awaited()
        assert pid in session.pending_confirmations      # 草稿保留，用户可重选

    @pytest.mark.asyncio
    async def test_select_blocked_entity_returns_403(self):
        """会话中途被禁的设备不能因为弹框里还留着就执行；403 不被压成 502。"""
        from app.core.database import Database
        from app.core.exceptions import AppException
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        await Database.get().emoji_pref_upsert("entity_operable", "light.b", "0")
        session = self._session()
        pid = self._draft(session)
        probe = AsyncMock()
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            with pytest.raises(AppException) as ei:
                await select_pending_devices(
                    pid, PendingSelectRequest(session_id="s1", entity_ids=["light.b"]),
                    container=self._container(session), current_user={"user_id": "u1"})
        assert ei.value.http_status == 403
        probe.assert_not_awaited()
        assert pid in session.pending_confirmations      # 解除限制后可直接重试

    @pytest.mark.asyncio
    async def test_select_unknown_pending_returns_404(self):
        from app.core.database import Database
        from app.core.exceptions import AppException
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        session = self._session()
        with patch("app.routes.ha_routes.call_with_probe", new=AsyncMock()):
            with pytest.raises(AppException) as ei:
                await select_pending_devices(
                    "sel-gone", PendingSelectRequest(session_id="s1", entity_ids=["light.a"]),
                    container=self._container(session), current_user={"user_id": "u1"})
        assert ei.value.http_status == 404

    @pytest.mark.asyncio
    async def test_select_foreign_session_forbidden(self):
        from app.core.database import Database
        from app.core.exceptions import AppException
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        session = self._session(user_id="someone_else")
        pid = self._draft(session)
        with pytest.raises(AppException) as ei:
            await select_pending_devices(
                pid, PendingSelectRequest(session_id="s1", entity_ids=["light.a"]),
                container=self._container(session), current_user={"user_id": "u1"})
        assert ei.value.http_status == 403

    @pytest.mark.asyncio
    async def test_select_missing_session_not_found(self):
        from app.core.database import Database
        from app.core.exceptions import AppException
        from app.routes.ha_routes import select_pending_devices
        from app.schema.api_schemas import PendingSelectRequest
        await Database.init()
        container = self._container(self._session())
        container.session_store.get_session = AsyncMock(return_value=None)
        with pytest.raises(AppException) as ei:
            await select_pending_devices(
                "sel-x", PendingSelectRequest(session_id="nope", entity_ids=["light.a"]),
                container=container, current_user={"user_id": "u1"})
        assert ei.value.http_status == 404

    @pytest.mark.asyncio
    async def test_cancel_removes_draft_without_executing(self):
        from app.core.database import Database
        from app.routes.ha_routes import cancel_pending_selection
        from app.schema.api_schemas import PendingConfirmRequest
        await Database.init()
        session = self._session()
        pid = self._draft(session)
        probe = AsyncMock()
        with patch("app.routes.ha_routes.call_with_probe", new=probe):
            result = await cancel_pending_selection(
                pid, PendingConfirmRequest(session_id="s1"),
                container=self._container(session), current_user={"user_id": "u1"})
        assert result.data["cancelled"] is True
        assert pid not in session.pending_confirmations
        probe.assert_not_awaited()
        assert session.model_messages == []          # 取消不污染会话历史

    @pytest.mark.asyncio
    async def test_cancel_unknown_returns_404(self):
        from app.core.database import Database
        from app.core.exceptions import AppException
        from app.routes.ha_routes import cancel_pending_selection
        from app.schema.api_schemas import PendingConfirmRequest
        await Database.init()
        with pytest.raises(AppException) as ei:
            await cancel_pending_selection(
                "sel-gone", PendingConfirmRequest(session_id="s1"),
                container=self._container(self._session()), current_user={"user_id": "u1"})
        assert ei.value.http_status == 404

    def test_both_endpoints_require_auth(self):
        """两个端点都挂路由级 get_current_user。

        全局 api_token_guard 中间件（app/main.py）只保证「调用方登录了」，给不出
        user_id；而这两个端点要 require_owned_session 校验会话归属，必须有当前
        用户身份。中间件还放行 APP_TOKEN（X-API-Token，无 JWT 身份）这条向后兼容
        路径，所以身份只能靠路由依赖拿。
        """
        import inspect

        from app.core.auth import get_current_user
        from app.routes import ha_routes
        for fn in (ha_routes.select_pending_devices, ha_routes.cancel_pending_selection):
            defaults = [p.default for p in inspect.signature(fn).parameters.values()]
            assert any(getattr(d, "dependency", None) is get_current_user for d in defaults), \
                f"{fn.__name__} 缺少 get_current_user 依赖"
