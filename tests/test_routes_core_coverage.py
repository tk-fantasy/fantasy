"""Routes coverage tests（A 组）— ha/session/user 路由的未覆盖分支。

风格沿用既有路由测试：直调路由协程 + mock 边界（container / Database /
HA client），需要落库的行为走 tests/test_database.py 的临时库隔离模式。
断言真实行为：状态码、响应 JSON、DB 副作用、mock 调用参数。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.api_models import ApiResponse
from app.core.exceptions import AppException
from app.utils.handlers import register_exception_handlers


def _mock_container(**overrides):
    c = MagicMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


@pytest.fixture
async def db(tmp_path):
    """临时 SQLite（test_database.py 隔离模式），路由里 Database.get() 直接可用。"""
    from app.core.database import Database

    Database._instance = None
    Database._db = None
    Database._write_lock = None
    with patch("app.core.database.DB_PATH", tmp_path / "routes_a.db"):
        instance = await Database.init()
        yield instance
    await Database.close()
    Database._instance = None
    Database._db = None
    Database._write_lock = None


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "http://ha:8123/api/")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


# ===================== ha_routes =====================

class TestHAEntitiesAndServices:
    async def test_ha_entities_wraps_failure_as_502(self):
        from app.routes.ha_routes import ha_entities

        svc = MagicMock()
        svc.get_all_devices = AsyncMock(side_effect=RuntimeError("conn refused"))
        with pytest.raises(AppException) as ei:
            await ha_entities(container=_mock_container(ha_service=svc))
        assert ei.value.code == "ha_error"
        assert ei.value.http_status == 502
        assert "Home Assistant 连接失败" in ei.value.message

    async def test_get_entity_services_ok_and_failure(self):
        from app.routes.ha_routes import get_entity_services, ha_services

        svc = MagicMock()
        svc.get_service_defs = AsyncMock(return_value={
            "light": {"turn_on": {}, "turn_off": {}},
            "switch": {"turn_on": {}},
        })
        result = await get_entity_services(container=_mock_container(ha_service=svc))
        assert result.data["services"]["light"] == ["turn_on", "turn_off"]

        svc.get_service_defs = AsyncMock(side_effect=RuntimeError("x"))
        with pytest.raises(AppException) as ei:
            await get_entity_services(container=_mock_container(ha_service=svc))
        assert ei.value.code == "ha_error"

        svc.get_service_defs = AsyncMock(side_effect=RuntimeError("x"))
        with pytest.raises(AppException) as ei:
            await ha_services(container=_mock_container(ha_service=svc))
        assert ei.value.code == "ha_error"
        assert ei.value.http_status == 502

    async def test_ha_history_ok_and_failure(self):
        from app.routes.ha_routes import ha_history

        client = MagicMock()
        client.get_history = AsyncMock(return_value=[{"s": 1}, {"s": 2}])
        result = await ha_history(
            filter_entity_id="light.a", hours=24, minimal=True,
            container=_mock_container(ha_client=client),
        )
        assert result.data["count"] == 2
        kwargs = client.get_history.await_args.kwargs
        assert kwargs["filter_entity_id"] == "light.a"
        assert kwargs["minimal"] is True

        client.get_history = AsyncMock(side_effect=RuntimeError("timeout"))
        with pytest.raises(AppException) as ei:
            await ha_history(
                filter_entity_id="light.a", hours=1, minimal=True,
                container=_mock_container(ha_client=client),
            )
        assert ei.value.code == "ha_error"
        assert ei.value.http_status == 502


class TestEntityAliasRoutes:
    async def test_get_and_set_alias_roundtrip(self, db):
        from app.routes.ha_routes import get_entity_aliases, set_entity_alias
        from app.schema.api_schemas import EntityAliasRequest

        assert (await get_entity_aliases()).data == {"aliases": {}}

        client = MagicMock()
        client.update_entity_name = AsyncMock()
        container = _mock_container(ha_client=client)
        container.ha_service.invalidate_states_cache = MagicMock()

        result = await set_entity_alias(
            EntityAliasRequest(entity_id="light.keting", alias="客厅灯"),
            container=container,
        )
        assert result.data == {"entity_id": "light.keting", "alias": "客厅灯"}
        client.update_entity_name.assert_awaited_once_with("light.keting", "客厅灯")
        aliases = (await get_entity_aliases()).data["aliases"]
        assert aliases == {"light.keting": "客厅灯"}
        container.ha_service.invalidate_states_cache.assert_called_once()

    async def test_empty_alias_clears_and_syncs_none_to_ha(self, db):
        from app.routes.ha_routes import get_entity_aliases, set_entity_alias
        from app.schema.api_schemas import EntityAliasRequest

        await db.emoji_pref_upsert("entity_alias", "light.keting", "旧名")
        client = MagicMock()
        client.update_entity_name = AsyncMock()

        await set_entity_alias(
            EntityAliasRequest(entity_id="light.keting", alias=""),
            container=_mock_container(ha_client=client),
        )
        client.update_entity_name.assert_awaited_once_with("light.keting", None)
        assert (await get_entity_aliases()).data["aliases"] == {}

    async def test_ha_sync_failure_rolls_back(self, db):
        from app.routes.ha_routes import get_entity_aliases, set_entity_alias
        from app.schema.api_schemas import EntityAliasRequest

        client = MagicMock()
        client.update_entity_name = AsyncMock(side_effect=RuntimeError("ha down"))
        with pytest.raises(AppException) as ei:
            await set_entity_alias(
                EntityAliasRequest(entity_id="light.keting", alias="新名"),
                container=_mock_container(ha_client=client),
            )
        assert ei.value.code == "ha_sync_failed"
        assert ei.value.http_status == 502
        # Aether 侧未写入，两边保持一致
        assert (await get_entity_aliases()).data["aliases"] == {}

    async def test_missing_entity_id_rejected(self, db):
        from app.routes.ha_routes import set_entity_alias
        from app.schema.api_schemas import EntityAliasRequest

        with pytest.raises(AppException) as ei:
            await set_entity_alias(
                EntityAliasRequest(entity_id="", alias="x"),
                container=_mock_container(),
            )
        assert ei.value.code == "missing_params"


class TestEntityNoteRoutes:
    async def test_set_note_upsert_and_refresh(self, db):
        from app.routes.ha_routes import get_entity_notes, set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        assert (await get_entity_notes()).data == {"notes": {}}

        container = _mock_container()
        container.catalog_refresh_fn = AsyncMock()
        result = await set_entity_note(
            EntityNoteRequest(entity_id="relay.door", note="ON=关门"),
            container=container,
        )
        assert result.data["note"] == "ON=关门"
        assert (await get_entity_notes()).data["notes"] == {"relay.door": "ON=关门"}
        await asyncio.sleep(0)
        container.catalog_refresh_fn.assert_called_once()

    async def test_empty_note_deletes(self, db):
        from app.routes.ha_routes import get_entity_notes, set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        await db.emoji_pref_upsert("entity_note", "relay.door", "旧备注")
        container = _mock_container()
        container.catalog_refresh_fn = None  # 无刷新回调也不报错
        await set_entity_note(
            EntityNoteRequest(entity_id="relay.door", note=""), container=container
        )
        assert (await get_entity_notes()).data["notes"] == {}

    async def test_missing_entity_id_rejected(self, db):
        from app.routes.ha_routes import set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        with pytest.raises(AppException) as ei:
            await set_entity_note(
                EntityNoteRequest(entity_id="", note="x"), container=_mock_container()
            )
        assert ei.value.code == "missing_params"


class TestEntityOperableRoutes:
    async def test_disable_and_restore(self, db):
        from app.routes.ha_routes import get_entity_operable, set_entity_operable
        from app.schema.api_schemas import EntityOperableRequest

        assert (await get_entity_operable()).data == {"disabled": {}}

        container = _mock_container()
        container.catalog_refresh_fn = AsyncMock()

        # 禁止 AI 操作 → 写黑名单
        result = await set_entity_operable(
            EntityOperableRequest(entity_id="light.a", operable=False),
            container=container,
        )
        assert result.data == {"entity_id": "light.a", "operable": False}
        assert (await get_entity_operable()).data["disabled"] == {"light.a": "0"}

        # 恢复 → 删除记录
        result = await set_entity_operable(
            EntityOperableRequest(entity_id="light.a", operable=True),
            container=container,
        )
        assert result.data["operable"] is True
        assert (await get_entity_operable()).data["disabled"] == {}
        await asyncio.sleep(0)
        assert container.catalog_refresh_fn.call_count == 2

    async def test_missing_entity_id_rejected(self, db):
        from app.routes.ha_routes import set_entity_operable
        from app.schema.api_schemas import EntityOperableRequest

        with pytest.raises(AppException) as ei:
            await set_entity_operable(
                EntityOperableRequest(entity_id="", operable=False),
                container=_mock_container(),
            )
        assert ei.value.code == "missing_params"


class TestActionMapRoutes:
    async def test_get_maps_skips_invalid_entries(self, db):
        from app.routes.ha_routes import get_action_maps

        await db.emoji_pref_upsert(
            "entity_action_map", "relay.a",
            json.dumps({"mappings": {"turn_on": {"target": "turn_off"}}}),
        )
        await db.emoji_pref_upsert("entity_action_map", "relay.b", "not-json{")
        await db.emoji_pref_upsert("entity_action_map", "relay.c", '{"no_mappings": 1}')

        maps = (await get_action_maps()).data["maps"]
        assert set(maps.keys()) == {"relay.a"}
        assert maps["relay.a"]["mappings"]["turn_on"]["target"] == "turn_off"

    async def test_set_map_success_and_cleanup(self, db):
        from app.routes.ha_routes import get_action_maps, set_action_map
        from app.schema.api_schemas import ActionMapRequest
        from app.services import semantic_map

        svc = MagicMock()
        svc.get_service_defs = AsyncMock(return_value={
            "relay": {"turn_on": {}, "turn_off": {}, "toggle": {}},
        })
        container = _mock_container(ha_service=svc)
        container.catalog_refresh_fn = AsyncMock()

        payload = ActionMapRequest(
            entity_id="relay.a",
            mappings={
                "turn_on": {"target": "turn_off", "description": "继电器反转"},
                "toggle": {"target": "toggle"},      # target==源 → 丢弃
                "bad": "not-a-dict",                  # 非 dict → 丢弃
            },
        )
        result = await set_action_map(payload, container=container)
        assert result.data["entity_id"] == "relay.a"
        stored = (await get_action_maps()).data["maps"]["relay.a"]
        assert stored["mappings"] == {
            "turn_on": {"target": "turn_off", "description": "继电器反转"}
        }
        await asyncio.sleep(0)
        container.catalog_refresh_fn.assert_called_once()

        # 空 mappings → 删除
        await set_action_map(
            ActionMapRequest(entity_id="relay.a", mappings={}), container=container
        )
        assert "relay.a" not in (await get_action_maps()).data["maps"]

        # 全部条目被过滤 → 等价删除
        await set_action_map(
            ActionMapRequest(entity_id="relay.a", mappings={"toggle": {"target": "toggle"}}),
            container=container,
        )
        assert "relay.a" not in (await get_action_maps()).data["maps"]

    async def test_set_map_rejects_invalid_target(self, db):
        from app.routes.ha_routes import set_action_map
        from app.schema.api_schemas import ActionMapRequest

        svc = MagicMock()
        svc.get_service_defs = AsyncMock(return_value={"relay": {"turn_on": {}}})
        with pytest.raises(AppException) as ei:
            await set_action_map(
                ActionMapRequest(
                    entity_id="relay.a",
                    mappings={"turn_on": {"target": "nonexistent"}},
                ),
                container=_mock_container(ha_service=svc),
            )
        assert ei.value.code == "invalid_target"
        assert ei.value.http_status == 400

    async def test_set_map_service_defs_failure(self, db):
        from app.routes.ha_routes import set_action_map
        from app.schema.api_schemas import ActionMapRequest

        svc = MagicMock()
        svc.get_service_defs = AsyncMock(return_value={})
        with pytest.raises(AppException) as ei:
            await set_action_map(
                ActionMapRequest(
                    entity_id="relay.a", mappings={"turn_on": {"target": "turn_off"}}
                ),
                container=_mock_container(ha_service=svc),
            )
        assert ei.value.code == "ha_error"
        assert ei.value.http_status == 502

    async def test_set_map_missing_entity_id(self, db):
        from app.routes.ha_routes import set_action_map
        from app.schema.api_schemas import ActionMapRequest

        with pytest.raises(AppException) as ei:
            await set_action_map(
                ActionMapRequest(entity_id="", mappings={}),
                container=_mock_container(),
            )
        assert ei.value.code == "missing_params"


class TestCallServiceRoute:
    async def test_short_entity_id_prefixed_and_recorded(self):
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        probe = AsyncMock(return_value={"ok": True})
        container = _mock_container(ha_client=MagicMock())
        container.ha_service.invalidate_states_cache = MagicMock()
        container.ha_service.get_states_snapshot = AsyncMock(return_value=[
            {"entity_id": "light.bulb", "attributes": {"friendly_name": "灯"}}
        ])

        with patch("app.routes.ha_routes.call_with_probe", probe), \
             patch("app.services.device_event_service.record_device_op", new=AsyncMock()) as rec:
            result = await ha_call_service(
                HAServiceCallRequest(domain="light", service="turn_on",
                                     entity_id="bulb", data={"brightness": 1}),
                container=container,
            )
        assert result.data["success"] is True
        probe.assert_awaited_once_with(container.ha_client, "light", "turn_on",
                                       "light.bulb", {"brightness": 1})
        container.ha_service.invalidate_states_cache.assert_called_once()
        rec.assert_awaited_once_with(
            ["light.bulb"], "turn_on", "手动", {"light.bulb": "灯"}
        )

    async def test_snapshot_failure_still_records_op(self):
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container(ha_client=MagicMock())
        container.ha_service.invalidate_states_cache = MagicMock()
        container.ha_service.get_states_snapshot = AsyncMock(
            side_effect=RuntimeError("x")
        )
        with patch("app.routes.ha_routes.call_with_probe", AsyncMock(return_value={})), \
             patch("app.services.device_event_service.record_device_op", new=AsyncMock()) as rec:
            result = await ha_call_service(
                HAServiceCallRequest(domain="light", service="turn_on",
                                     entity_id="light.a"),
                container=container,
            )
        assert result.data["success"] is True
        assert rec.await_args.args[3] == {}  # name_of 回退空表

    async def test_recorder_failure_swallowed(self):
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container(ha_client=MagicMock())
        container.ha_service.invalidate_states_cache = MagicMock()
        with patch("app.routes.ha_routes.call_with_probe", AsyncMock(return_value={})), \
             patch("app.services.device_event_service.record_device_op",
                   new=AsyncMock(side_effect=RuntimeError("db down"))):
            result = await ha_call_service(
                HAServiceCallRequest(domain="light", service="turn_on",
                                     entity_id="light.a"),
                container=container,
            )
        assert result.data["success"] is True

    async def test_probe_failure_wrapped_502(self):
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container(ha_client=MagicMock())
        container.ha_service.invalidate_states_cache = MagicMock()
        with patch("app.routes.ha_routes.call_with_probe",
                   AsyncMock(side_effect=RuntimeError("boom"))), \
             pytest.raises(AppException) as ei:
            await ha_call_service(
                HAServiceCallRequest(domain="light", service="turn_on",
                                     entity_id="light.a"),
                container=container,
            )
        assert ei.value.code == "ha_error"
        assert ei.value.http_status == 502


class TestClassifyHAError:
    def test_classification_matrix(self):
        from app.routes.ha_routes import _classify_ha_error

        info = _classify_ha_error(_http_status_error(401))
        assert info == {
            "reason": "unauthorized",
            "detail": "Token 无效或已过期（URL 可达，请检查 Token）",
        }
        info = _classify_ha_error(_http_status_error(500))
        assert info["reason"] == "error"
        assert "HTTP 500" in info["detail"]

        conn = httpx.ConnectError("refused")
        info = _classify_ha_error(conn)
        assert info["reason"] == "unreachable"
        info = _classify_ha_error(httpx.TimeoutException("t/o"))
        assert info["reason"] == "unreachable"
        info = _classify_ha_error(RuntimeError("other"))
        assert info == {"reason": "error", "detail": "other"}


class TestHAConfigRoutes:
    async def test_get_ha_config_masks_token(self):
        import app.core.config as cfg
        from app.routes.ha_routes import get_ha_config

        cfg.CONFIG["ha"] = {"url": "http://ha:8123", "token": "eyJabc.def.ghi"}
        data = (await get_ha_config()).data
        assert data == {
            "url": "http://ha:8123",
            "token_set": True,
            "token_preview": "eyJa****.ghi",
        }
        cfg.CONFIG["ha"] = {"url": "http://ha:8123", "token": "short"}
        assert (await get_ha_config()).data["token_preview"] == "****"
        cfg.CONFIG["ha"] = {}
        data = (await get_ha_config()).data
        assert data["token_set"] is False and data["token_preview"] == ""

    async def test_set_config_rejects_bad_scheme(self):
        from app.routes.ha_routes import set_ha_config
        from app.schema.api_schemas import HAConfigRequest

        result = await set_ha_config(
            HAConfigRequest(url="ftp://ha:8123"),
            current_user={"user_id": "u1"},
            container=_mock_container(),
        )
        assert result.data["saved"] is False
        assert "只允许" in result.message

    async def test_set_config_rejects_bad_token(self):
        from app.routes.ha_routes import set_ha_config
        from app.schema.api_schemas import HAConfigRequest

        probe = MagicMock()
        probe.get_states = AsyncMock(side_effect=_http_status_error(401))
        probe.close = AsyncMock()
        with patch("app.routes.ha_routes.HomeAssistantClient", return_value=probe):
            result = await set_ha_config(
                HAConfigRequest(
                    url="http://ha:8123",
                    token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
                ),
                current_user={"user_id": "u1"},
                container=_mock_container(),
            )
        assert result.code == "ha_error"
        assert result.data["saved"] is False
        assert result.data["reason"] == "unauthorized"
        # connect 失败 → unreachable
        probe.get_states = AsyncMock(side_effect=httpx.ConnectError("no"))
        with patch("app.routes.ha_routes.HomeAssistantClient", return_value=probe):
            result = await set_ha_config(
                HAConfigRequest(
                    url="http://ha:8123",
                    token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
                ),
                current_user={"user_id": "u1"},
                container=_mock_container(),
            )
        assert result.data["reason"] == "unreachable"

    async def test_set_config_url_only_success(self):
        from app.routes.ha_routes import set_ha_config
        from app.schema.api_schemas import HAConfigRequest

        old_client = MagicMock()
        old_client.close = AsyncMock()
        container = _mock_container(ha_client_ref=[old_client])
        container.ha_service = MagicMock()

        with patch("app.main.sync_ha_runtime_refs") as sync_refs:
            result = await set_ha_config(
                HAConfigRequest(url="http://new-ha:8123/"),
                current_user={"user_id": "u1"},
                container=container,
            )
        assert result.data["saved"] is True
        assert result.data["url"] == "http://new-ha:8123"
        assert result.data["token_set"] is False
        old_client.close.assert_awaited_once()
        assert container.ha_client_ref[0] is not old_client
        sync_refs.assert_called_once()

    async def test_set_config_with_token_success(self):
        from app.routes.ha_routes import set_ha_config
        from app.schema.api_schemas import HAConfigRequest

        old_client = MagicMock()
        old_client.close = AsyncMock()
        container = _mock_container(ha_client_ref=[old_client])
        container.ha_service = MagicMock()

        probe = MagicMock()
        probe.get_states = AsyncMock(return_value=[{"e": 1}])
        probe.close = AsyncMock()
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
        with patch("app.routes.ha_routes.HomeAssistantClient", return_value=probe) as cls, \
             patch("app.main.sync_ha_runtime_refs"):
            result = await set_ha_config(
                HAConfigRequest(url="http://ha:8123", token=token),
                current_user={"user_id": "u1"},
                container=container,
            )
        assert result.data["saved"] is True
        assert result.data["token_set"] is True
        assert result.data["token_preview"] == token[:4] + "****" + token[-4:]
        # 两次构造：验证用临时 probe + 保存后的新 client
        assert cls.call_count == 2
        assert cls.call_args_list[0] == __import__("unittest").mock.call(
            base_url="http://ha:8123", token=token
        )


class TestHATestAndModelRoutes:
    async def test_spawn_catalog_refresh_swallows_spawn_failure(self):
        """refresh_fn 返回非协程 → create_task 抛 TypeError → 只记警告。"""
        from app.routes.ha_routes import _spawn_catalog_refresh

        with patch("app.routes.ha_routes.logger") as mock_logger:
            _spawn_catalog_refresh(lambda: "not-a-coroutine")
        mock_logger.warning.assert_called_once()

    async def test_test_connection_ok(self):
        from app.routes.ha_routes import test_ha_connection

        client = MagicMock()
        client.get_states = AsyncMock(return_value=[1, 2, 3])
        result = await test_ha_connection(container=_mock_container(ha_client=client))
        assert result.data == {"connected": True, "entity_count": 3}

    async def test_test_connection_unauthorized(self):
        from app.routes.ha_routes import test_ha_connection

        client = MagicMock()
        client.get_states = AsyncMock(side_effect=_http_status_error(401))
        result = await test_ha_connection(container=_mock_container(ha_client=client))
        assert result.code == "ha_error"
        assert result.data["connected"] is False
        assert result.data["reason"] == "unauthorized"

    async def test_test_connection_generic_error(self):
        from app.routes.ha_routes import test_ha_connection

        client = MagicMock()
        client.get_states = AsyncMock(side_effect=RuntimeError("boom"))
        result = await test_ha_connection(container=_mock_container(ha_client=client))
        assert result.data["reason"] == "error"
        assert result.data["connected"] is False

    async def test_models_test_route(self):
        from app.routes.ha_routes import test_model_connection_route
        from app.schema.api_schemas import ModelTestRequest

        fake = AsyncMock(return_value={"ok": True, "latency_ms": 5})
        with patch("app.services.model_test_service.test_model_connection", fake):
            result = await test_model_connection_route(
                ModelTestRequest(base_url="http://m:1", model="gpt", role="chat")
            )
        assert result.data["ok"] is True
        assert fake.await_args.kwargs["model"] == "gpt"


class TestUniqueSettingsRoutes:
    async def test_get_returns_defaults_when_unset(self):
        from app.routes.ha_routes import get_unique_settings
        from app.services.prompt_service import DEFAULT_PERSONA, GUIDELINES

        data = (await get_unique_settings()).data
        assert data["persona"] == DEFAULT_PERSONA
        assert data["guidelines"] == GUIDELINES
        assert data["persona_custom"] is False
        assert data["guidelines_custom"] is False

    async def test_get_returns_custom_persona(self):
        import app.core.config as cfg
        from app.routes.ha_routes import get_unique_settings

        cfg.CONFIG["chat_assistant"] = {"persona": "猫娘", "guidelines": "g"}
        data = (await get_unique_settings()).data
        assert data["persona"] == "猫娘"
        assert data["persona_custom"] is True
        assert data["guidelines_custom"] is True

    async def test_set_persona(self):
        from app.routes.ha_routes import set_unique_settings
        from app.schema.api_schemas import UniqueSettingsRequest

        result = await set_unique_settings(
            UniqueSettingsRequest(persona="  管家  ")
        )
        assert result.data["persona"] == "管家"
        assert result.data["persona_custom"] is True
        assert result.data["guidelines_custom"] is False


# ===================== session_routes =====================

class TestSessionRouteGuards:
    async def test_chat_dispatcher_not_ready_503(self):
        from app.routes.session_routes import chat
        from app.schema.api_schemas import ChatRequest

        container = MagicMock()
        container.dispatcher = None
        with pytest.raises(AppException) as ei:
            await chat(
                ChatRequest(query="hi", session_id="s"),
                {"user_id": "u1"}, container=container,
            )
        assert ei.value.http_status == 503
        assert ei.value.code == "dispatcher_not_ready"

    async def test_delete_all_sessions(self):
        from app.routes.session_routes import delete_all_sessions

        store = MagicMock()
        store.delete_all = AsyncMock(return_value=7)
        result = await delete_all_sessions(
            {"user_id": "u1"}, container=_mock_container(session_store=store)
        )
        assert result.data == {"deleted": True, "count": 7}
        store.delete_all.assert_awaited_once_with(user_id="u1")

    async def test_get_session_ownership_matrix(self):
        from app.routes.session_routes import get_session

        store = MagicMock()
        store.get_session = AsyncMock(return_value=None)
        with pytest.raises(AppException) as ei:
            await get_session("s1", {"user_id": "u1"},
                              container=_mock_container(session_store=store))
        assert (ei.value.code, ei.value.http_status) == ("session_not_found", 404)

        other = MagicMock()
        other.user_id = "someone-else"
        store.get_session = AsyncMock(return_value=other)
        with pytest.raises(AppException) as ei:
            await get_session("s1", {"user_id": "u1"},
                              container=_mock_container(session_store=store))
        assert (ei.value.code, ei.value.http_status) == ("forbidden", 403)

        mine = MagicMock()
        mine.user_id = "u1"
        mine.detail.return_value = {"session_id": "s1", "messages": []}
        store.get_session = AsyncMock(return_value=mine)
        result = await get_session("s1", {"user_id": "u1"},
                                   container=_mock_container(session_store=store))
        assert result.data["session_id"] == "s1"

    async def test_delete_session_not_found(self):
        from app.routes.session_routes import delete_session

        store = MagicMock()
        session = MagicMock()
        session.user_id = "u1"
        store.get_session = AsyncMock(return_value=session)
        store.delete_session = AsyncMock(return_value=False)
        with pytest.raises(AppException) as ei:
            await delete_session("s1", {"user_id": "u1"},
                                 container=_mock_container(session_store=store))
        assert ei.value.code == "session_not_found"

    async def test_fork_session(self):
        from app.routes.session_routes import fork_session

        store = MagicMock()
        session = MagicMock()
        session.user_id = "u1"
        store.get_session = AsyncMock(return_value=session)
        store.fork_session = AsyncMock(return_value=None)
        with pytest.raises(AppException) as ei:
            await fork_session("s1", {"message_id": "m1"}, {"user_id": "u1"},
                               container=_mock_container(session_store=store))
        assert ei.value.code == "session_not_found"

        forked = MagicMock()
        forked.summary.return_value = {"session_id": "s2"}
        store.fork_session = AsyncMock(return_value=forked)
        result = await fork_session("s1", {"message_id": "m1"}, {"user_id": "u1"},
                                    container=_mock_container(session_store=store))
        assert result.data["session_id"] == "s2"
        store.fork_session.assert_awaited_once_with("s1", "m1", user_id="u1")

    async def test_undo_message(self):
        from app.routes.session_routes import undo_message

        store = MagicMock()
        session = MagicMock()
        session.user_id = "u1"
        session.visible_messages.return_value = [{"role": "user"}]
        store.get_session = AsyncMock(return_value=session)
        store.undo_last_message = AsyncMock(return_value=False)
        with pytest.raises(AppException) as ei:
            await undo_message("s1", {"user_id": "u1"},
                               container=_mock_container(session_store=store))
        assert ei.value.code == "undo_failed"

        store.undo_last_message = AsyncMock(return_value=True)
        result = await undo_message("s1", {"user_id": "u1"},
                                    container=_mock_container(session_store=store))
        assert result.data["undone"] is True
        assert result.data["messages"] == [{"role": "user"}]

    async def test_clear_session(self):
        from app.routes.session_routes import clear_session

        store = MagicMock()
        session = MagicMock()
        session.user_id = "u1"
        store.get_session = AsyncMock(return_value=session)
        store.clear_messages = AsyncMock(return_value=False)
        with pytest.raises(AppException) as ei:
            await clear_session("s1", {"user_id": "u1"},
                                container=_mock_container(session_store=store))
        assert ei.value.code == "session_not_found"

        store.clear_messages = AsyncMock(return_value=True)
        result = await clear_session("s1", {"user_id": "u1"},
                                     container=_mock_container(session_store=store))
        assert result.data == {"cleared": True}

    async def test_compress_session(self):
        from app.routes.session_routes import compress_session

        store = MagicMock()
        session = MagicMock()
        session.user_id = "u1"
        session.summaries = {"0": "摘要"}
        session.model_messages = ["m1", "m2"]
        store.get_session = AsyncMock(return_value=session)
        store.store_session = AsyncMock()
        summarizer = MagicMock()
        summarizer.refresh_summaries = AsyncMock()

        result = await compress_session(
            "s1", {"user_id": "u1"},
            container=_mock_container(session_store=store,
                                      summarization_service=summarizer),
        )
        assert result.data["compressed"] is True
        assert result.data["summaries"] == {"0": "摘要"}
        assert result.data["message_count"] == 2
        summarizer.refresh_summaries.assert_awaited_once_with(session, user_id="u1")
        store.store_session.assert_awaited_once_with(session)


# ===================== user_routes =====================

class TestUserRouteGuards:
    async def test_get_llm_keys_user_not_found(self):
        from app.routes.user_routes import get_user_llm_keys

        db = AsyncMock()
        db.user_get_by_username = AsyncMock(return_value=None)
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             pytest.raises(AppException) as ei:
            await get_user_llm_keys("ghost", {"user_id": "u1"})
        assert (ei.value.code, ei.value.http_status) == ("user_not_found", 404)

    async def test_get_providers_user_not_found(self):
        from app.routes.user_routes import get_user_providers

        db = AsyncMock()
        db.user_get_by_username = AsyncMock(return_value=None)
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             pytest.raises(AppException) as ei:
            await get_user_providers("ghost", {"user_id": "u1"})
        assert (ei.value.code, ei.value.http_status) == ("user_not_found", 404)

    async def test_save_llm_keys_user_not_found(self):
        from app.routes.user_routes import save_user_llm_keys
        from app.schema.api_schemas import UserLLMKeysRequest

        db = AsyncMock()
        db.user_get_by_username = AsyncMock(return_value=None)
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             pytest.raises(AppException) as ei:
            await save_user_llm_keys(
                "ghost", UserLLMKeysRequest(keys=[]), {"user_id": "u1"},
                container=MagicMock(),
            )
        assert (ei.value.code, ei.value.http_status) == ("user_not_found", 404)

    async def test_save_providers_user_not_found(self):
        from app.routes.user_routes import save_user_providers
        from app.schema.api_schemas import UserProvidersRequest

        db = AsyncMock()
        db.user_get_by_username = AsyncMock(return_value=None)
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             pytest.raises(AppException) as ei:
            await save_user_providers(
                "ghost", UserProvidersRequest(providers={}), {"user_id": "u1"}
            )
        assert (ei.value.code, ei.value.http_status) == ("user_not_found", 404)


class TestSaveUserLlmKeys:
    def _setup(self, target_id="u1"):
        db = AsyncMock()
        db.user_get_by_username = AsyncMock(
            return_value={"id": target_id, "username": "tester"}
        )
        return db

    async def test_idor_rejected(self):
        from app.routes.user_routes import save_user_llm_keys
        from app.schema.api_schemas import UserLLMKeysRequest

        db = self._setup(target_id="someone-else")
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             pytest.raises(AppException) as ei:
            await save_user_llm_keys(
                "tester", UserLLMKeysRequest(keys=[]), {"user_id": "u1"},
                container=MagicMock(),
            )
        assert (ei.value.code, ei.value.http_status) == ("forbidden", 403)

    async def test_save_own_keys_updates_env_memory_and_db(self):
        from app.routes.user_routes import save_user_llm_keys
        from app.schema.api_schemas import UserLLMKeysRequest

        db = self._setup()
        container = MagicMock()
        keys = [
            {"id": "a", "api_key_env": "LLM_KEY_A", "api_key": "v1"},
            {"id": "b", "api_key_env": "EVIL_NAME", "api_key": "v2"},  # 不合规范 → 跳过
            {"id": "c"},                                               # 无 key → 跳过
        ]
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             patch("app.routes.user_routes.update_memory_config") as mem, \
             patch("app.routes.user_routes.write_secrets") as wsec:
            result = await save_user_llm_keys(
                "tester", UserLLMKeysRequest(keys=keys), {"user_id": "u1"},
                container=container,
            )
        assert result.data == {"saved": True, "count": 3}
        wsec.assert_called_once_with({"LLM_KEY_A": "v1"})
        mem.assert_called_once_with("llm_keys", keys)
        # DB 写入的是完整 JSON
        saved = db.user_setting_set.await_args.args
        assert saved[0] == "u1" and saved[1] == "llm_keys"
        assert json.loads(saved[2]) == keys
        container.reload_all_clients.assert_called_once()

    async def test_reload_failure_still_saves(self):
        from app.routes.user_routes import save_user_llm_keys
        from app.schema.api_schemas import UserLLMKeysRequest

        db = self._setup()
        container = MagicMock()
        container.reload_all_clients = MagicMock(side_effect=RuntimeError("x"))
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             patch("app.routes.user_routes.update_memory_config"), \
             patch("app.routes.user_routes.write_secrets"):
            result = await save_user_llm_keys(
                "tester", UserLLMKeysRequest(keys=[{"id": "a"}]),
                {"user_id": "u1"}, container=container,
            )
        assert result.data["saved"] is True


class TestSaveUserProviders:
    async def test_idor_rejected(self):
        from app.routes.user_routes import save_user_providers
        from app.schema.api_schemas import UserProvidersRequest

        db = AsyncMock()
        db.user_get_by_username = AsyncMock(
            return_value={"id": "someone-else", "username": "tester"}
        )
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             pytest.raises(AppException) as ei:
            await save_user_providers(
                "tester", UserProvidersRequest(providers={}), {"user_id": "u1"}
            )
        assert (ei.value.code, ei.value.http_status) == ("forbidden", 403)

    async def test_save_own_providers(self):
        from app.routes.user_routes import save_user_providers
        from app.schema.api_schemas import UserProvidersRequest

        db = AsyncMock()
        db.user_get_by_username = AsyncMock(
            return_value={"id": "u1", "username": "tester"}
        )
        providers = {"chat": {"key_id": "k1"}}
        with patch("app.routes.user_routes.Database.get", return_value=db), \
             patch("app.routes.user_routes.update_memory_config") as mem:
            result = await save_user_providers(
                "tester", UserProvidersRequest(providers=providers), {"user_id": "u1"}
            )
        assert result.data == {"saved": True}
        mem.assert_called_once_with("providers", providers)
        saved = db.user_setting_set.await_args.args
        assert (saved[0], saved[1]) == ("u1", "providers")
        assert json.loads(saved[2]) == providers


# ===================== llm_key_routes =====================

class TestListLlmKeysRoute:
    async def test_empty_when_no_user_setting(self):
        from app.routes.llm_key_routes import list_llm_keys

        db = AsyncMock()
        db.user_setting_get = AsyncMock(return_value=None)
        with patch("app.routes.llm_key_routes.Database.get", return_value=db):
            result = await list_llm_keys({"user_id": "u1"})
        assert result.data == []

    async def test_masks_keys_and_reports_env_state(self, monkeypatch):
        from app.routes.llm_key_routes import list_llm_keys

        monkeypatch.setenv("LLM_KEY_A", "secret")
        monkeypatch.delenv("LLM_KEY_B", raising=False)
        keys = [
            {"id": "a", "base_url": "http://u", "model": "m", "type": "chat",
             "api_key_env": "LLM_KEY_A", "api_key": "stored"},
            {"id": "b", "base_url": "http://u", "model": "m", "type": "chat",
             "api_key_env": "LLM_KEY_B"},
            {"id": "c", "base_url": "http://u", "model": "m", "type": "chat",
             "api_key": "plain"},
        ]
        db = AsyncMock()
        db.user_setting_get = AsyncMock(return_value=json.dumps(keys))
        with patch("app.routes.llm_key_routes.Database.get", return_value=db):
            result = await list_llm_keys({"user_id": "u1"})
        out = {k["id"]: k for k in result.data}
        assert out["a"]["api_key_set"] is True   # env 变量存在
        assert out["b"]["api_key_set"] is False  # env 未设置
        assert out["c"]["api_key_set"] is True   # 无 env 名 → 看 DB 明文
        assert "api_key" not in out["c"]  # 明文永不返回


class TestUpsertLlmKeyRoute:
    def _patches(self, test_ok=True):
        test_conn = AsyncMock(return_value={"ok": test_ok, "error": "boom"})
        return (
            patch("app.routes.llm_key_routes.test_model_connection", test_conn),
            patch("app.core.config.write_secrets"),
            patch("app.services.llm_key_service.reload_key_pools"),
            patch("app.services.llm_key_service.sync_llm_keys_to_current_user",
                  new=AsyncMock()),
        )

    async def test_invalid_type_rejected(self):
        from app.routes.llm_key_routes import upsert_llm_key_route
        from app.schema.api_schemas import LLMKeyRequest

        with pytest.raises(AppException) as ei:
            await upsert_llm_key_route(
                LLMKeyRequest(base_url="http://x", model="m", type="bad"),
                {"user_id": "u1"}, container=MagicMock(),
            )
        assert ei.value.code == "llm_key_invalid"

    async def test_new_embed_key_local_url_normalized(self):
        from app.routes.llm_key_routes import upsert_llm_key_route
        from app.schema.api_schemas import LLMKeyRequest

        p1, p2, p3, p4 = self._patches()
        with p1 as test_conn, p2, p3 as reload_pools, p4 as sync_user:
            result = await upsert_llm_key_route(
                LLMKeyRequest(base_url="http://127.0.0.1:11434", model="bge-m3",
                              type="embed", api_key="sk-test"),
                {"user_id": "u1"}, container=MagicMock(),
            )
        saved = {k["id"]: k for k in result.data}
        entry = next(iter(saved.values()))
        assert entry["base_url"] == "http://127.0.0.1:11434/v1"  # 本地地址自动补 /v1
        assert entry["chat_path"] == "" and entry["embed_path"] == "/embeddings"
        test_conn.assert_awaited_once()  # 新增 key 自动测连
        reload_pools.assert_called_once()
        sync_user.assert_awaited_once()

    async def test_new_chat_key_test_failure_rejected(self):
        from app.routes.llm_key_routes import upsert_llm_key_route
        from app.schema.api_schemas import LLMKeyRequest

        p1, p2, p3, p4 = self._patches(test_ok=False)
        with p1, p2, p3, p4, pytest.raises(AppException) as ei:
            await upsert_llm_key_route(
                LLMKeyRequest(base_url="https://api.example.com", model="gpt",
                              type="chat", api_key="sk-bad"),
                {"user_id": "u1"}, container=MagicMock(),
            )
        assert ei.value.code == "llm_key_test_failed"
        assert ei.value.http_status == 400


class TestDeleteLlmKeyRoute:
    async def test_delete_delegates_and_reloads(self):
        from app.routes.llm_key_routes import delete_llm_key_route

        remaining = [{"id": "other"}]
        with patch("app.routes.llm_key_routes.delete_llm_key",
                   return_value=remaining) as del_key, \
             patch("app.services.llm_key_service.reload_key_pools") as reload_pools, \
             patch("app.services.llm_key_service.sync_llm_keys_to_current_user",
                   new=AsyncMock()) as sync_user:
            result = await delete_llm_key_route("k1", {"user_id": "u1"},
                                                container=MagicMock())
        assert result.data == remaining
        del_key.assert_called_once_with("k1")
        reload_pools.assert_called_once()
        sync_user.assert_awaited_once()


class TestLlmSettingsRoutes:
    async def test_get_settings_merges_user_providers(self):
        from app.routes.llm_key_routes import get_llm_settings

        container = MagicMock()
        container.llm_settings_service.current_settings.return_value = {
            "chat": {"key_id": "old", "max_concurrency": 8, "thinking": False,
                     "use_global": True},
        }
        container.llm_settings_service.warnings.return_value = ["w1"]
        with patch("app.services.llm_key_service.get_user_providers",
                   new=AsyncMock(return_value={
                       "chat": {"key_id": "k9", "max_concurrency": 4,
                                "thinking": True, "custom": 1},
                   })):
            result = await get_llm_settings({"user_id": "u1"}, container=container)
        current = result.data["current"]
        # chat 被用户配置覆盖（保留用户自定义字段）
        assert current["chat"]["key_id"] == "k9"
        assert current["chat"]["thinking"] is True
        assert current["chat"]["custom"] == 1
        # summary/stt 无用户配置 → 默认值
        assert current["summary"] == {
            "key_id": None, "max_concurrency": 8, "thinking": False,
            "use_global": False,
        }
        assert "stt" in current
        assert result.data["warnings"] == ["w1"]

    async def test_set_settings_per_user_chat_with_global(self):
        from app.routes.llm_key_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        container = MagicMock()
        container.dispatcher.invalidate_user_agent = AsyncMock()
        with patch("app.services.llm_key_service.save_user_provider",
                   new=AsyncMock()) as save:
            result = await set_llm_settings(
                LLMSettingsRequest(role="chat", key_id="k1", max_concurrency=-3,
                                   thinking=True, use_global=True),
                {"user_id": "u1"}, container=container,
            )
        applied = result.data["applied"]
        # use_global=True → key_id 清空；并发数下限钳到 1
        assert applied["key_id"] == ""
        assert applied["use_global"] is True
        assert applied["thinking"] is True
        assert applied["max_concurrency"] == 1
        save.assert_awaited_once()
        assert save.await_args.args[0] == "u1"
        container.dispatcher.invalidate_user_agent.assert_awaited_once_with("u1")

    async def test_set_settings_zero_concurrency_falls_back_to_default(self):
        """0 为 falsy：`or 8` 分支 → 回退默认并发 8（实际行为契约）。"""
        from app.routes.llm_key_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        container = MagicMock()
        container.dispatcher.invalidate_user_agent = AsyncMock()
        with patch("app.services.llm_key_service.save_user_provider", new=AsyncMock()):
            result = await set_llm_settings(
                LLMSettingsRequest(role="chat", key_id="k1", max_concurrency=0),
                {"user_id": "u1"}, container=container,
            )
        assert result.data["applied"]["max_concurrency"] == 8

    async def test_set_settings_per_user_summary_minimal(self):
        from app.routes.llm_key_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        container = MagicMock()
        container.dispatcher.invalidate_user_agent = AsyncMock()
        with patch("app.services.llm_key_service.save_user_provider",
                   new=AsyncMock()) as save:
            result = await set_llm_settings(
                LLMSettingsRequest(role="summary", key_id="k2", use_global=False),
                {"user_id": "u1"}, container=container,
            )
        applied = result.data["applied"]
        assert applied["key_id"] == "k2"
        assert applied["use_global"] is False
        assert "thinking" not in applied  # thinking 未传 → 不写
        assert save.await_args.args[2] == "k2"

    async def test_set_settings_global_role_syncs_to_db(self):
        from app.routes.llm_key_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        container = MagicMock()
        container.llm_settings_service.apply = MagicMock(
            return_value={"role": "vision", "applied": True}
        )
        db = AsyncMock()
        with patch("app.routes.llm_key_routes.Database.get", return_value=db):
            result = await set_llm_settings(
                LLMSettingsRequest(role="vision", key_id="k3"),
                {"user_id": "u1"}, container=container,
            )
        assert result.data == {"role": "vision", "applied": True}
        container.llm_settings_service.apply.assert_called_once()
        saved = db.user_setting_set.await_args.args
        assert (saved[0], saved[1]) == ("u1", "providers")

    async def test_set_settings_global_role_db_failure_swallowed(self):
        from app.routes.llm_key_routes import set_llm_settings
        from app.schema.api_schemas import LLMSettingsRequest

        container = MagicMock()
        container.llm_settings_service.apply = MagicMock(return_value={"ok": 1})
        db = AsyncMock()
        db.user_setting_set = AsyncMock(side_effect=RuntimeError("db down"))
        with patch("app.routes.llm_key_routes.Database.get", return_value=db):
            result = await set_llm_settings(
                LLMSettingsRequest(role="embed", key_id="k4"),
                {"user_id": "u1"}, container=container,
            )
        assert result.data == {"ok": 1}


class TestLlmStatusRoute:
    async def test_status_matrix(self):
        from app.routes.llm_key_routes import get_llm_status

        user_key = {
            "api_key": "uk", "model": "user-model", "base_url": "http://user",
            "chat_path": "/c", "embed_path": "/e",
        }
        global_key = {
            "api_key": "gk", "model": "global-model", "base_url": "http://global",
            "chat_path": "/c", "embed_path": "/e",
        }
        empty_global = {"api_key": "", "model": "m", "base_url": "http://g"}

        async def resolve_user(role, user_id):
            return user_key if role == "chat" else None

        def resolve_global(role):
            return {"summary": empty_global, "vision": None, "embed": global_key}.get(role)

        test_conn = AsyncMock(return_value={"ok": False, "error": "timeout"})

        with patch("app.routes.llm_key_routes.resolve_key_for_role_user", resolve_user), \
             patch("app.routes.llm_key_routes.resolve_key_for_role", resolve_global), \
             patch("app.routes.llm_key_routes.test_model_connection", test_conn):
            result = await get_llm_status({"user_id": "u1"})

        roles = result.data["roles"]
        assert set(roles.keys()) == {"chat", "summary", "vision", "embed"}
        # chat：用户 key 优先
        assert roles["chat"]["source"] == "user"
        assert roles["chat"]["model"] == "user-model"
        # summary：用户无 → 全局 key 无 api_key → 未配置
        assert roles["summary"]["source"] == "global"
        assert roles["summary"]["error"] == "未配置可用的 API Key"
        # vision：全局也无 → 未配置
        assert roles["vision"]["error"] == "未配置可用的 API Key"
        # embed：全局有 key 但测连失败
        assert roles["embed"]["source"] == "global"
        assert roles["embed"]["connected"] is False
        assert roles["embed"]["error"] == "timeout"


# ===================== mcp_routes =====================

class TestMcpRoutes:
    def _fresh_rebuild(self, monkeypatch, agent):
        """路由函数在调用时 `from ..main import _rebuild_lock`，换新锁避免跨事件循环绑定。"""
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "_rebuild_lock", asyncio.Lock())
        monkeypatch.setattr(main_mod, "_rebuild_agent", agent)

    async def test_list_servers(self):
        from app.routes.mcp_routes import list_mcp_servers

        mgr = MagicMock()
        mgr.list_external_servers.return_value = [{"name": "fs"}]
        tool = MagicMock()
        tool.description = "read file"
        mgr._tools = {"fs_read": tool}
        result = await list_mcp_servers(container=_mock_container(mcp_client_manager=mgr))
        assert result.data["servers"] == [{"name": "fs"}]
        assert result.data["tools"] == [{"name": "fs_read", "description": "read file"}]

    async def test_connect_rejected_when_not_whitelisted(self):
        from app.routes.mcp_routes import connect_mcp_server
        from app.schema.api_schemas import MCPConnectRequest

        with patch("app.routes.mcp_routes.get_config", return_value=[]):
            with pytest.raises(AppException) as ei:
                await connect_mcp_server(
                    MCPConnectRequest(name="evil", cmd="rm -rf /", args=[]),
                    container=_mock_container(mcp_client_manager=MagicMock()),
                )
        assert (ei.value.code, ei.value.http_status) == ("mcp_not_whitelisted", 403)

    async def test_connect_success_rebuilds_agent(self, monkeypatch):
        from app.routes.mcp_routes import connect_mcp_server
        from app.schema.api_schemas import MCPConnectRequest

        agent = AsyncMock()
        self._fresh_rebuild(monkeypatch, agent)
        mgr = MagicMock()
        mgr.connect_external_server = AsyncMock(return_value=["t1", "t2"])
        with patch("app.routes.mcp_routes.get_config",
                   return_value=[{"name": "files", "cmd": "mcp-files"}]):
            result = await connect_mcp_server(
                MCPConnectRequest(name="files", cmd="mcp-files", args=["--a"]),
                container=_mock_container(mcp_client_manager=mgr),
            )
        assert result.data == {"connected": True, "name": "files", "tools": 2}
        agent.assert_awaited_once()

    async def test_connect_timeout(self, monkeypatch):
        from app.routes.mcp_routes import connect_mcp_server
        from app.schema.api_schemas import MCPConnectRequest

        self._fresh_rebuild(monkeypatch, AsyncMock())
        mgr = MagicMock()
        mgr.connect_external_server = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("app.routes.mcp_routes.get_config",
                   return_value=[{"name": "files", "cmd": "mcp-files"}]), \
             pytest.raises(AppException) as ei:
            await connect_mcp_server(
                MCPConnectRequest(name="files", cmd="mcp-files", args=[]),
                container=_mock_container(mcp_client_manager=mgr),
            )
        assert (ei.value.code, ei.value.http_status) == ("mcp_timeout", 504)

    async def test_connect_generic_error(self, monkeypatch):
        from app.routes.mcp_routes import connect_mcp_server
        from app.schema.api_schemas import MCPConnectRequest

        self._fresh_rebuild(monkeypatch, AsyncMock())
        mgr = MagicMock()
        mgr.connect_external_server = AsyncMock(side_effect=RuntimeError("spawn fail"))
        with patch("app.routes.mcp_routes.get_config",
                   return_value=[{"name": "files", "cmd": "mcp-files"}]), \
             pytest.raises(AppException) as ei:
            await connect_mcp_server(
                MCPConnectRequest(name="files", cmd="mcp-files", args=[]),
                container=_mock_container(mcp_client_manager=mgr),
            )
        assert (ei.value.code, ei.value.http_status) == ("mcp_error", 502)
        assert "spawn fail" in ei.value.message

    async def test_disconnect_success_and_not_found(self, monkeypatch):
        from app.routes.mcp_routes import disconnect_mcp_server

        agent = AsyncMock()
        self._fresh_rebuild(monkeypatch, agent)
        mgr = MagicMock()
        mgr.disconnect_server = AsyncMock(return_value=True)
        result = await disconnect_mcp_server(
            "files", container=_mock_container(mcp_client_manager=mgr)
        )
        assert result.data == {"disconnected": True, "name": "files"}
        agent.assert_awaited_once()

        mgr.disconnect_server = AsyncMock(return_value=False)
        with pytest.raises(AppException) as ei:
            await disconnect_mcp_server(
                "nope", container=_mock_container(mcp_client_manager=mgr)
            )
        assert (ei.value.code, ei.value.http_status) == ("not_found", 404)


# ===================== auth_routes（TestClient 走真实 HTTP 语义） =====================

@pytest.fixture()
def auth_client():
    """迷你 FastAPI 挂 auth 路由 + 全局异常处理；Database.get 换 AsyncMock。"""
    from app.routes import auth_routes
    from app.utils.handlers import register_exception_handlers

    auth_routes._register_limiter._requests.clear()
    auth_routes._login_limiter._requests.clear()
    db = AsyncMock()
    app = FastAPI()
    app.include_router(auth_routes.router)
    register_exception_handlers(app)
    with patch("app.core.database.Database.get", return_value=db):
        yield TestClient(app), db, auth_routes


class TestAuthRegisterRoute:
    def test_first_user_becomes_admin_and_gets_cookies(self, auth_client):
        client, db, _ = auth_client
        db.user_get_by_username = AsyncMock(return_value=None)
        db.user_count = AsyncMock(return_value=0)
        db.user_create = AsyncMock()

        resp = client.post("/auth/register",
                           json={"username": "alice", "password": "password123"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["user"]["is_admin"] == 1
        assert body["data"]["user"]["username"] == "alice"
        # 认证 cookie 已下发
        set_cookies = resp.headers.get_list("set-cookie")
        assert any("aether_token=" in c for c in set_cookies)
        assert any("aether_refresh_token=" in c for c in set_cookies)
        # user_settings 初始化了 llm_keys 和 providers
        assert db.user_setting_set.await_count == 2

    def test_duplicate_username_rejected(self, auth_client):
        client, db, _ = auth_client
        db.user_get_by_username = AsyncMock(return_value={"id": "x"})
        resp = client.post("/auth/register",
                           json={"username": "alice", "password": "password123"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "username_exists"

    def test_rate_limited(self, auth_client):
        client, db, routes = auth_client
        db.user_get_by_username = AsyncMock(return_value=None)
        for _ in range(3):
            routes._register_limiter.check("testclient")
        resp = client.post("/auth/register",
                           json={"username": "bob", "password": "password123"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "rate_limit_exceeded"


class TestAuthLoginRoute:
    def test_success_sets_cookies(self, auth_client):
        from app.core.auth import hash_password

        client, db, _ = auth_client
        db.user_get_by_username = AsyncMock(return_value={
            "id": "u1", "username": "alice",
            "password_hash": hash_password("pw123"),
            "display_name": "Alice", "is_admin": 1,
        })
        resp = client.post("/auth/login", json={"username": "alice", "password": "pw123"})
        assert resp.status_code == 200
        assert resp.json()["data"]["user"]["username"] == "alice"
        assert any("aether_token=" in c for c in resp.headers.get_list("set-cookie"))

    def test_rate_limited(self, auth_client):
        client, db, routes = auth_client
        for _ in range(5):
            routes._login_limiter.check("testclient")
        resp = client.post("/auth/login", json={"username": "alice", "password": "x"})
        assert resp.status_code == 429
        assert resp.json()["code"] == "rate_limit_exceeded"


class TestAuthRefreshRoute:
    def test_missing_cookie_rejected(self, auth_client):
        client, _, _ = auth_client
        resp = client.post("/auth/refresh")
        assert resp.status_code == 401
        assert resp.json()["code"] == "missing_refresh_token"

    def test_access_token_rejected_as_refresh(self, auth_client):
        from app.core.auth import create_access_token

        client, _, _ = auth_client
        client.cookies = {"aether_refresh_token": create_access_token("u1", "a")}
        resp = client.post("/auth/refresh")
        assert resp.status_code == 401
        assert resp.json()["code"] == "invalid_refresh_token"

    def test_deleted_user_rejected(self, auth_client):
        from app.core.auth import create_refresh_token

        client, db, _ = auth_client
        db.user_get_by_id = AsyncMock(return_value=None)
        client.cookies = {"aether_refresh_token": create_refresh_token("ghost")}
        resp = client.post("/auth/refresh")
        assert resp.status_code == 401
        assert resp.json()["code"] == "user_not_found"

    def test_valid_refresh_rotates_cookies(self, auth_client):
        from app.core.auth import create_refresh_token

        client, db, _ = auth_client
        db.user_get_by_id = AsyncMock(
            return_value={"id": "u1", "username": "alice", "display_name": "A"}
        )
        client.cookies = {"aether_refresh_token": create_refresh_token("u1")}
        resp = client.post("/auth/refresh")
        assert resp.status_code == 200
        set_cookies = " ".join(resp.headers.get_list("set-cookie"))
        assert "aether_token=" in set_cookies
        assert "aether_refresh_token=" in set_cookies


class TestAuthLogoutRoute:
    def test_revokes_valid_tokens_and_clears_cookies(self, auth_client):
        from app.core.auth import (
            create_access_token,
            create_refresh_token,
            verify_token,
        )
        from app.core.exceptions import AppException

        client, _, _ = auth_client
        access = create_access_token("u1", "alice")
        refresh = create_refresh_token("u1")
        client.cookies = {"aether_token": access, "aether_refresh_token": refresh}
        resp = client.post("/auth/logout")
        assert resp.status_code == 200
        # token 已入黑名单：verify 拒绝
        with pytest.raises(AppException):
            verify_token(access)
        with pytest.raises(AppException):
            verify_token(refresh)
        # cookie 被清除（delete cookie 头）
        set_cookies = " ".join(resp.headers.get_list("set-cookie"))
        assert "aether_token=" in set_cookies
        assert "Max-Age=0" in set_cookies or "max-age=0" in set_cookies

    def test_invalid_tokens_swallowed_still_clears(self, auth_client):
        client, _, _ = auth_client
        client.cookies = {"aether_token": "garbage", "aether_refresh_token": "junk"}
        resp = client.post("/auth/logout")
        assert resp.status_code == 200
        set_cookies = " ".join(resp.headers.get_list("set-cookie"))
        assert "aether_refresh_token=" in set_cookies


class TestAuthMeRoute:
    def test_user_not_found_404(self, auth_client):
        from app.core.auth import get_current_user

        client, db, _ = auth_client
        db.user_get_by_id = AsyncMock(return_value=None)
        client.app.dependency_overrides[get_current_user] = lambda: {
            "user_id": "ghost", "username": "ghost"}
        try:
            resp = client.get("/auth/me")
        finally:
            client.app.dependency_overrides.pop(get_current_user, None)
        assert resp.status_code == 404
        assert resp.json()["code"] == "user_not_found"


# ===================== setup_routes =====================

class TestSetupRoutes:
    async def test_index_redirects_to_landing(self):
        from app.routes.setup_routes import index

        resp = await index()
        assert resp.status_code == 307
        assert resp.headers["location"] == "/landing"

    async def test_favicon_serves_build_artifact(self):
        from app.routes.setup_routes import favicon

        resp = await favicon()
        assert resp.status_code == 200
        assert resp.media_type == "image/x-icon"

    async def test_favicon_fallback_204(self, monkeypatch):
        from app.routes.setup_routes import favicon

        monkeypatch.setattr("pathlib.Path.is_file", lambda self: False)
        resp = await favicon()
        assert resp.status_code == 204

    async def test_setup_status_invalid_token_falls_back_global(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers, req.cookies, req.query_params = {}, {}, {}

        with patch.object(setup_routes, "extract_token_from_request",
                          return_value="bad-token"), \
             patch.object(setup_routes, "get_config",
                          side_effect=lambda p, d=None: {"llm_keys": [], "ha": {}, "home": {}}.get(p, d)):
            result = await setup_routes.setup_status(
                req, container=_mock_container(ha_client=MagicMock())
            )
        assert result.data["has_llm_key"] is False
        assert result.data["setup_complete"] is False

    async def test_setup_status_user_level_settings_win(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers, req.cookies, req.query_params = {}, {}, {}
        db = AsyncMock()

        async def setting(user_id, key):
            if key == "llm_keys":
                return json.dumps([{"id": "k1"}])
            if key == "home_info":
                return json.dumps({"home_name": "我的家"})
            return None

        db.user_setting_get = AsyncMock(side_effect=setting)
        container = _mock_container(ha_client=MagicMock())
        container.ha_client.get_states = AsyncMock(return_value=[{"e": 1}, {"e": 2}])

        with patch.object(setup_routes, "extract_token_from_request",
                          return_value="tok"), \
             patch.object(setup_routes, "verify_token",
                          return_value={"sub": "u1", "username": "u"}), \
             patch.object(setup_routes, "get_config",
                          side_effect=lambda p, d=None: {"ha": {"url": "http://ha", "token": "t"}}.get(p, d)), \
             patch("app.core.database.Database.get", return_value=db):
            result = await setup_routes.setup_status(req, container=container)

        data = result.data
        assert data["has_llm_key"] is True
        assert data["llm_key_count"] == 1
        assert data["ha_configured"] is True and data["ha_connected"] is True
        assert data["has_home_info"] is True
        assert data["setup_complete"] is True

    async def test_setup_status_db_errors_swallowed(self):
        from app.routes import setup_routes

        req = MagicMock()
        req.headers, req.cookies, req.query_params = {}, {}, {}
        db = AsyncMock()
        db.user_setting_get = AsyncMock(side_effect=RuntimeError("db down"))

        with patch.object(setup_routes, "extract_token_from_request",
                          return_value="tok"), \
             patch.object(setup_routes, "verify_token",
                          return_value={"sub": "u1", "username": "u"}), \
             patch.object(setup_routes, "get_config",
                          side_effect=lambda p, d=None: {
                              "llm_keys": [], "ha": {},
                              "home": {"home_name": "全局家"},
                          }.get(p, d)), \
             patch("app.core.database.Database.get", return_value=db):
            result = await setup_routes.setup_status(
                req, container=_mock_container(ha_client=MagicMock())
            )
        # 用户级读取失败 → 回退全局：llm 无、home 有
        assert result.data["has_llm_key"] is False
        assert result.data["has_home_info"] is True

    async def test_setup_ha_connection_failure(self, monkeypatch):
        from app.routes import setup_routes

        monkeypatch.delenv("HA_URL", raising=False)
        container = _mock_container()
        container.ha_client.get_states = AsyncMock(side_effect=RuntimeError("refused"))

        with patch.object(setup_routes, "extract_token_from_request",
                          return_value="tok"), \
             patch.object(setup_routes, "verify_token",
                          return_value={"sub": "u1"}):
            result = await setup_routes.setup_ha(
                setup_routes.HASetupRequest(url="http://ha:8123/", token="tok"),
                request=MagicMock(), container=container, admin={"user_id": "u1"},
            )
        assert result.data["ha_connected"] is False
        assert result.data["entity_count"] == 0
        assert result.data["url"] == "http://ha:8123"
        assert result.data["url_overridden_by_env"] is False

    async def test_setup_ha_env_url_override(self, monkeypatch):
        from app.routes import setup_routes

        monkeypatch.setenv("HA_URL", "http://ha-in-docker:8123")
        container = _mock_container()
        container.ha_client.get_states = AsyncMock(return_value=[{"e": 1}])

        with patch.object(setup_routes, "extract_token_from_request",
                          return_value="tok"), \
             patch.object(setup_routes, "verify_token",
                          return_value={"sub": "u1"}):
            result = await setup_routes.setup_ha(
                setup_routes.HASetupRequest(url="http://ha:8123", token="tok"),
                request=MagicMock(), container=container, admin={"user_id": "u1"},
            )
        assert result.data["ha_connected"] is True
        assert result.data["entity_count"] == 1
        assert result.data["url"] == "http://ha-in-docker:8123"
        assert result.data["url_overridden_by_env"] is True


# ===================== global_config_routes =====================

def _fake_request(ip: str = "t-ip") -> MagicMock:
    req = MagicMock()
    req.client.host = ip
    return req


def _pw_patches():
    return (
        patch("app.routes.global_config_routes.llm_key_service.is_secondary_password_set",
              return_value=True),
        patch("app.routes.global_config_routes.llm_key_service.verify_secondary_password"),
    )


class TestGlobalPasswordRoutes:
    async def test_password_status(self):
        from app.routes.global_config_routes import get_global_password_status

        with patch("app.routes.global_config_routes.llm_key_service.is_secondary_password_set",
                   return_value=True):
            result = await get_global_password_status()
        assert result.data == {"set": True}

    async def test_set_password_first_time(self):
        from app.routes.global_config_routes import set_global_password
        from app.schema.api_schemas import SecondaryPasswordSetupRequest

        with patch("app.routes.global_config_routes.llm_key_service.is_secondary_password_set",
                   return_value=False), \
             patch("app.routes.global_config_routes.set_secondary_password_hash") as setter:
            result = await set_global_password(
                SecondaryPasswordSetupRequest(password="secret-pw"),
                current_user={"user_id": "admin"},
            )
        assert result.data == {"set": True}
        setter.assert_called_once()
        assert setter.call_args.args[0] != "secret-pw"  # 存的是哈希

    async def test_reset_password_not_set_409(self):
        from app.routes.global_config_routes import reset_global_password

        with patch("app.routes.global_config_routes.llm_key_service.is_secondary_password_set",
                   return_value=False), \
             pytest.raises(AppException) as ei:
            await reset_global_password(current_user={"user_id": "admin"})
        assert (ei.value.code, ei.value.http_status) == ("secondary_password_not_set", 409)

    async def test_reset_password_clears(self):
        from app.routes.global_config_routes import reset_global_password

        with patch("app.routes.global_config_routes.llm_key_service.is_secondary_password_set",
                   return_value=True), \
             patch("app.routes.global_config_routes.set_secondary_password_hash") as setter:
            result = await reset_global_password(current_user={"user_id": "admin"})
        assert result.data == {"reset": True}
        setter.assert_called_once_with("")


class TestGlobalLlmKeyRoutes:
    def _set_existing_keys(self, keys):
        import app.core.config as cfg

        cfg.CONFIG["llm_keys"] = keys

    async def test_upsert_rejects_invalid_type(self):
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        p1, p2 = _pw_patches()
        with p1, p2, pytest.raises(AppException) as ei:
            await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="http://x", model="m", type="nope",
                                    password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=MagicMock(),
            )
        assert ei.value.code == "llm_key_invalid"

    async def test_upsert_new_key_requires_api_key(self):
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        p1, p2 = _pw_patches()
        with p1, p2, pytest.raises(AppException) as ei:
            await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="http://x", model="m", type="chat",
                                    api_key="", password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=MagicMock(),
            )
        assert ei.value.code == "llm_key_missing_api_key"

    async def test_upsert_new_key_test_failure(self):
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        p1, p2 = _pw_patches()
        with p1, p2, \
             patch("app.routes.global_config_routes.test_model_connection",
                   AsyncMock(return_value={"ok": False, "error": "refused"})), \
             pytest.raises(AppException) as ei:
            await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="http://x", model="m", type="vision",
                                    api_key="sk", password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=MagicMock(),
            )
        assert ei.value.code == "llm_key_test_failed"

    async def test_upsert_new_chat_key_hot_reloads(self, monkeypatch):
        import app.main as main_mod
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        agent = AsyncMock()
        monkeypatch.setattr(main_mod, "_rebuild_lock", asyncio.Lock())
        monkeypatch.setattr(main_mod, "_rebuild_agent", agent)
        container = MagicMock()

        p1, p2 = _pw_patches()
        with p1, p2, \
             patch("app.routes.global_config_routes.test_model_connection",
                   AsyncMock(return_value={"ok": True})), \
             patch("app.routes.global_config_routes.write_secrets") as wsec:
            result = await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="http://127.0.0.1:8000", model="qwen",
                                    type="chat", api_key="sk-1", password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=container,
            )
        assert result.data["restart_required"] is False
        keys = result.data["keys"]
        assert len(keys) == 1
        entry = keys[0]
        assert entry["base_url"] == "http://127.0.0.1:8000/v1"  # 本地地址补 /v1
        assert entry["api_key_env"].startswith("LLM_KEY_")
        assert "api_key" not in entry  # 明文不落 config
        wsec.assert_called_once_with({entry["api_key_env"]: "sk-1"})
        agent.assert_awaited_once()

    async def test_upsert_existing_key_without_api_key_keeps_env(self):
        import app.core.config as cfg
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        self._set_existing_keys([{
            "id": "exist", "base_url": "http://e", "model": "m", "type": "embed",
            "chat_path": "", "embed_path": "/embeddings",
            "api_key_env": "LLM_KEY_EXIST",
        }])
        container = MagicMock()
        p1, p2 = _pw_patches()
        with p1, p2, \
             patch("app.routes.global_config_routes.write_secrets") as wsec:
            result = await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="http://e", model="m2", type="embed",
                                    api_key="", id="exist", password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=container,
            )
        wsec.assert_not_called()  # 留空 = 不改密钥
        entry = result.data["keys"][0]
        assert entry["id"] == "exist"
        assert entry["api_key_env"] == "LLM_KEY_EXIST"  # 沿用原 env 名
        assert cfg.CONFIG["llm_keys"][0]["model"] == "m2"

    async def test_upsert_existing_key_with_new_api_key_writes_secret(self):
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        self._set_existing_keys([{
            "id": "exist", "base_url": "http://e", "model": "m", "type": "stt",
            "chat_path": "", "embed_path": "", "api_key_env": "LLM_KEY_EXIST",
        }])
        p1, p2 = _pw_patches()
        with p1, p2, \
             patch("app.routes.global_config_routes.write_secrets") as wsec:
            await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="http://e", model="whisper", type="stt",
                                    api_key="sk-new", id="exist", password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=MagicMock(),
            )
        wsec.assert_called_once_with({"LLM_KEY_EXIST": "sk-new"})

    async def test_upsert_hot_reload_failure_sets_restart_flag(self, monkeypatch):
        import app.main as main_mod
        from app.routes.global_config_routes import upsert_global_llm_key_route
        from app.schema.api_schemas import GlobalLLMKeyRequest

        monkeypatch.setattr(main_mod, "_rebuild_lock", asyncio.Lock())
        monkeypatch.setattr(main_mod, "_rebuild_agent",
                            AsyncMock(side_effect=RuntimeError("reload boom")))

        p1, p2 = _pw_patches()
        with p1, p2, \
             patch("app.routes.global_config_routes.test_model_connection",
                   AsyncMock(return_value={"ok": True})), \
             patch("app.routes.global_config_routes.write_secrets"):
            result = await upsert_global_llm_key_route(
                GlobalLLMKeyRequest(base_url="https://api.x.com", model="gpt",
                                    type="chat", api_key="sk", password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=MagicMock(),
            )
        assert result.data["restart_required"] is True

    async def test_delete_global_key(self):
        from app.routes.global_config_routes import delete_global_llm_key_route
        from app.schema.api_schemas import SecondaryPasswordVerifyRequest

        self._set_existing_keys([
            {"id": "k1", "base_url": "http://a", "model": "m", "type": "chat",
             "chat_path": "/c", "embed_path": ""},
            {"id": "k2", "base_url": "http://b", "model": "m", "type": "embed",
             "chat_path": "", "embed_path": "/e"},
        ])
        p1, p2 = _pw_patches()
        with p1, p2 as verify:
            result = await delete_global_llm_key_route(
                "k1", SecondaryPasswordVerifyRequest(password="pw"),
                request=_fake_request(), current_user={"user_id": "admin"},
                container=MagicMock(),
            )
        assert [k["id"] for k in result.data["keys"]] == ["k2"]
        verify.assert_called_once_with("pw")  # verify_secondary_password 收到请求里的密码

    async def test_get_global_settings(self):
        from app.routes.global_config_routes import get_global_llm_settings

        container = MagicMock()
        container.llm_settings_service.current_settings.return_value = {"chat": {}}
        container.llm_settings_service.warnings.return_value = []
        result = await get_global_llm_settings(
            {"user_id": "u1"}, container=container
        )
        assert result.data == {"current": {"chat": {}}, "warnings": []}

    async def test_set_global_settings_vision_no_reload(self, monkeypatch):
        import app.main as main_mod
        from app.routes.global_config_routes import set_global_llm_settings
        from app.schema.api_schemas import GlobalLLMSettingsRequest

        agent = AsyncMock()
        monkeypatch.setattr(main_mod, "_rebuild_lock", asyncio.Lock())
        monkeypatch.setattr(main_mod, "_rebuild_agent", agent)
        container = MagicMock()
        container.llm_settings_service.apply = MagicMock(
            return_value={"role": "vision", "applied": True}
        )
        p1, p2 = _pw_patches()
        with p1, p2:
            result = await set_global_llm_settings(
                GlobalLLMSettingsRequest(role="vision", key_id="k9", password="pw"),
                request=_fake_request(), current_user={"user_id": "u1"},
                container=container,
            )
        assert result.data == {"role": "vision", "applied": True,
                               "restart_required": False}
        agent.assert_not_awaited()  # 非 chat 角色不热重载

    async def test_set_global_settings_chat_reload_failure(self, monkeypatch):
        import app.main as main_mod
        from app.routes.global_config_routes import set_global_llm_settings
        from app.schema.api_schemas import GlobalLLMSettingsRequest

        monkeypatch.setattr(main_mod, "_rebuild_lock", asyncio.Lock())
        monkeypatch.setattr(main_mod, "_rebuild_agent",
                            AsyncMock(side_effect=RuntimeError("x")))
        container = MagicMock()
        container.llm_settings_service.apply = MagicMock(return_value={"ok": 1})
        p1, p2 = _pw_patches()
        with p1, p2:
            result = await set_global_llm_settings(
                GlobalLLMSettingsRequest(role="chat", key_id="k1", password="pw"),
                request=_fake_request(), current_user={"user_id": "u1"},
                container=container,
            )
        assert result.data["restart_required"] is True

    async def test_write_rate_limited(self):
        from app.routes.global_config_routes import (
            _check_write_limited,
            _write_limiter,
        )

        _write_limiter._requests.clear()
        for _ in range(10):
            _write_limiter.check("limited-ip")
        with pytest.raises(AppException) as ei:
            _check_write_limited(_fake_request("limited-ip"))
        assert (ei.value.code, ei.value.http_status) == ("rate_limited", 429)


