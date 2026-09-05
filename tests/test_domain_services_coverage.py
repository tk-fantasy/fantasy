"""Coverage-gap tests for service modules (batch B).

Covers: weather_service, rule_service, scene_service, scheduler_service,
stt_service, schedule_parser_service — branches not exercised by the
existing per-module test files. All external boundaries (LLM clients,
HTTP APIs, DB) are mocked or pointed at tmp_path.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AppException


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _reset_db_singleton():
    """Isolate the Database singleton per test (same pattern as test_database.py)."""
    from app.core.database import Database

    Database._instance = None
    Database._db = None
    Database._write_lock = None
    yield
    if Database._db:
        await Database._db.close()
    Database._instance = None
    Database._db = None
    Database._write_lock = None


@pytest.fixture
async def temp_db(tmp_path: Path):
    from app.core.database import Database

    with patch("app.core.database.DB_PATH", tmp_path / "test.db"):
        instance = await Database.init()
        yield instance


# ---------------------------------------------------------------------------
# weather_service
# ---------------------------------------------------------------------------

def _weather_cfg() -> dict:
    """Config dict handed to weather_service.get_config patches."""
    return {
        "weather": {
            "host": "devapi.example.com",
            "kid": "kid-1",
            "sub": "pro",
            "private_key": "",
        },
        "home": {},
    }


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, resp):
        self.resp = resp
        self.get_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        self.get_calls.append({"url": url, "headers": headers, "params": params})
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def _cfg_getter(cfg):
    return lambda path, default=None: cfg.get(path, default)


class TestGenerateJwt:
    def _make_pem(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pem_der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pub_raw = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return pem, pem_der, pub_raw

    @staticmethod
    def _b64d(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def test_jwt_from_b64_key(self):
        """裸 base64 私钥 → 包装成 PEM 再签名；JWT 三段结构可被公钥验证。"""
        from app.services import weather_service
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        _, pem_der, pub_raw = self._make_pem()
        cfg = _weather_cfg()
        # 服务端期望的是 DER 的 base64（无 PEM 头），由它自己包装成 PEM
        cfg["weather"]["private_key"] = base64.b64encode(pem_der).decode()
        with patch.object(weather_service, "get_config", _cfg_getter(cfg)):
            token = weather_service._generate_jwt()

        parts = token.split(".")
        assert len(parts) == 3
        header = json.loads(self._b64d(parts[0]))
        assert header == {"alg": "EdDSA", "kid": "kid-1"}
        payload = json.loads(self._b64d(parts[1]))
        assert payload["sub"] == "pro"
        assert payload["exp"] - payload["iat"] == 930
        Ed25519PublicKey.from_public_bytes(pub_raw).verify(
            self._b64d(parts[2]), f"{parts[0]}.{parts[1]}".encode()
        )

    def test_jwt_from_pem_key(self):
        """PEM 格式私钥直接使用（-----BEGIN 分支）。"""
        from app.services import weather_service

        pem, _, _ = self._make_pem()
        cfg = _weather_cfg()
        cfg["weather"]["private_key"] = pem.decode()
        with patch.object(weather_service, "get_config", _cfg_getter(cfg)):
            token = weather_service._generate_jwt()
        assert token.count(".") == 2

    def test_jwt_missing_key_raises(self):
        from app.services import weather_service

        cfg = _weather_cfg()
        cfg["weather"]["private_key"] = ""
        with patch.object(weather_service, "get_config", _cfg_getter(cfg)):
            with pytest.raises(ValueError, match="private_key"):
                weather_service._generate_jwt()


class TestQweatherRequest:
    async def test_success(self):
        from app.services import weather_service

        client = _FakeAsyncClient(_FakeResp({"ok": 1}))
        cfg = _weather_cfg()
        with patch.object(weather_service, "get_config", _cfg_getter(cfg)), \
             patch.object(weather_service, "_generate_jwt", return_value="h.p.s"), \
             patch.object(weather_service, "new_client", return_value=client):
            out = await weather_service._qweather_request("/geo/v2/city/lookup", {"location": "上海"})
        assert out == {"ok": 1}
        assert client.get_calls[0]["url"] == "https://devapi.example.com/geo/v2/city/lookup"
        assert client.get_calls[0]["headers"]["Authorization"] == "Bearer h.p.s"
        assert client.get_calls[0]["params"] == {"location": "上海"}

    async def test_no_host_raises(self):
        from app.services import weather_service

        cfg = _weather_cfg()
        cfg["weather"]["host"] = ""
        with patch.object(weather_service, "get_config", _cfg_getter(cfg)):
            with pytest.raises(ValueError, match="host"):
                await weather_service._qweather_request("/x")

    async def test_retries_then_raises_last_error(self):
        from app.services import weather_service

        client = _FakeAsyncClient(RuntimeError("boom"))
        cfg = _weather_cfg()
        with patch.object(weather_service, "get_config", _cfg_getter(cfg)), \
             patch.object(weather_service, "_generate_jwt", return_value="h.p.s"), \
             patch.object(weather_service, "new_client", return_value=client), \
             patch("asyncio.sleep", new=AsyncMock()) as fake_sleep:
            with pytest.raises(RuntimeError, match="boom"):
                await weather_service._qweather_request("/x")
        # 初次 + 2 次重试 = 3 次请求，2 次 backoff sleep
        assert len(client.get_calls) == 3
        assert fake_sleep.await_count == 2


class TestGetWeatherBranches:
    async def test_no_location_and_no_home_city(self):
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        cfg = _weather_cfg()  # home 空
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "get_config", _cfg_getter(cfg)):
            MockDB.get.return_value = db
            out = await weather_service.get_weather(None)
        assert "请先在设置中配置家庭地址" in out["error"]

    async def test_no_location_home_province_differs(self):
        """省 != 市 → 用 省市 拼接查询。"""
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        cfg = _weather_cfg()
        cfg["home"] = {"province": "广东省", "city": "深圳", "district": "南山"}
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "get_config", _cfg_getter(cfg)), \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = Exception("stop-here")
            out = await weather_service.get_weather(None)
        assert out["location"] == "广东省深圳"
        api.assert_awaited_once_with("/geo/v2/city/lookup", {"location": "广东省深圳"})

    async def test_no_location_municipality_city_only(self):
        """直辖市（省市同名）只用市名。"""
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        cfg = _weather_cfg()
        cfg["home"] = {"province": "上海市", "city": "上海市", "district": "宝山"}
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "get_config", _cfg_getter(cfg)), \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = Exception("stop-here")
            await weather_service.get_weather(None)
        api.assert_awaited_once_with("/geo/v2/city/lookup", {"location": "上海市"})

    async def test_corrupt_cache_falls_through_to_api(self):
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value="{not-json")
        geo = {"location": [{"id": "ID1", "name": "深圳", "adm1": "广东省"}]}
        now = {"now": {"temp": "30"}}
        idx = {"daily": []}
        db.kv_set = AsyncMock()
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = [geo, now, idx]
            out = await weather_service.get_weather("深圳")
        assert out["temperature"] == "30"
        assert out["location"] == "广东省深圳"  # adm1 + name 拼接
        db.kv_set.assert_awaited_once()

    async def test_adm1_contains_name_collapses(self):
        """locations 为 dict 时包装成 list；adm1 包含 name → 不产生 "上海市上海"。"""
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        geo = {"location": {"id": "ID1", "name": "上海", "adm1": "上海市"}}
        now = {"now": {"temp": "25"}}
        idx = {"daily": []}
        db.kv_set = AsyncMock()
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = [geo, now, idx]
            out = await weather_service.get_weather("上海")
        assert out["location"] == "上海"
        assert out["location_id"] == "ID1"

    async def test_geo_returns_empty(self):
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.return_value = {"location": []}
            out = await weather_service.get_weather("不存在的地方")
        assert "找不到地点" in out["error"]

    async def test_weather_task_fails_indices_still_rendered(self):
        """并行请求中 weather 失败（gather return_exceptions）→ now 字段为空但指数保留。"""
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        db.kv_set = AsyncMock()
        geo = {"location": [{"id": "ID1", "name": "深圳", "adm1": ""}]}  # adm1 空 → full_name=name
        idx = {"daily": [{"type": "1", "name": "运动", "level": "1",
                          "category": "适宜", "text": "适合跑步"}]}
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = [geo, Exception("weather down"), idx]
            out = await weather_service.get_weather("深圳")
        assert out["temperature"] == ""  # now 缺失
        assert out["location"] == "深圳"
        assert out["indices"][0]["name"] == "运动"
        db.kv_set.assert_awaited_once()


class TestIpLocateListBranch:
    async def test_ip_locate_list_location(self):
        from app.services import weather_service

        geo = {"location": [{"name": "宝山", "adm1": "上海", "adm2": "上海",
                             "lat": "31.40", "lon": "121.49", "id": "101020300"}]}
        with patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            api.return_value = geo
            out = await weather_service.ip_locate()
        assert out["id"] == "101020300"
        assert out["lat"] == "31.40"


class TestCityLookup:
    async def test_success_list(self):
        from app.services import weather_service

        geo = {"location": [{"name": "上海", "adm1": "上海市", "adm2": "上海",
                             "lat": "31.2", "lon": "121.4", "id": "1"}]}
        with patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            api.return_value = geo
            out = await weather_service.city_lookup("上海")
        assert out["cities"][0]["name"] == "上海"

    async def test_dict_location_wrapped(self):
        from app.services import weather_service

        geo = {"location": {"name": "北京", "id": "2"}}
        with patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            api.return_value = geo
            out = await weather_service.city_lookup("北京")
        assert out["cities"] == [{"name": "北京", "adm1": "", "adm2": "",
                                  "lat": "", "lon": "", "id": "2"}]

    async def test_error_returns_cities_empty(self):
        from app.services import weather_service

        with patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            api.side_effect = RuntimeError("net down")
            out = await weather_service.city_lookup("x")
        assert out["cities"] == []
        assert "城市查询失败" in out["error"]


class TestGetWeatherIndices:
    async def test_cache_hit(self):
        from app.services import weather_service

        db = MagicMock()
        cached = {"indices": {"location": "深圳", "indices": [{"name": "运动"}]},
                  "cached_at": time.time()}
        db.kv_get = AsyncMock(return_value=json.dumps(cached))
        with patch.object(weather_service, "Database") as MockDB:
            MockDB.get.return_value = db
            out = await weather_service.get_weather_indices("深圳")
        assert out["indices"][0]["name"] == "运动"

    async def test_corrupt_cache_then_full_flow(self):
        """坏缓存 → GeoAPI(list) → indices → 写缓存。"""
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value="???")
        db.kv_set = AsyncMock()
        geo = {"location": [{"id": "ID9", "name": "深圳"}]}
        idx = {"daily": [{"type": "5", "name": "洗车", "level": "2",
                          "category": "较适宜", "text": "可以洗车"}]}
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = [geo, idx]
            out = await weather_service.get_weather_indices("深圳")
        assert out["location_id"] == "ID9"
        assert out["indices"][0]["name"] == "洗车"
        db.kv_set.assert_awaited_once()

    async def test_geo_returns_dict(self):
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        db.kv_set = AsyncMock()
        geo = {"location": {"id": "IDD", "name": "x"}}
        idx = {"daily": []}
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = [geo, idx]
            out = await weather_service.get_weather_indices("浦东")
        assert out["location_id"] == "IDD"
        assert out["indices"] == []

    async def test_numeric_location_skips_geo(self):
        """Location ID / 坐标（数字+逗号）直接查指数，不走 GeoAPI。"""
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        db.kv_set = AsyncMock()
        idx = {"daily": [{"type": "1", "name": "运动", "level": "3",
                          "category": "较不宜", "text": "下雨"}]}
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.return_value = idx
            out = await weather_service.get_weather_indices("121.47,31.23")
        assert out["location_id"] == "121.47,31.23"
        api.assert_awaited_once()  # 只有 indices 一次调用

    async def test_api_error(self):
        from app.services import weather_service

        db = MagicMock()
        db.kv_get = AsyncMock(return_value=None)
        with patch.object(weather_service, "Database") as MockDB, \
             patch.object(weather_service, "_qweather_request", new_callable=AsyncMock) as api:
            MockDB.get.return_value = db
            api.side_effect = RuntimeError("down")
            out = await weather_service.get_weather_indices("深圳")
        assert "获取指数失败" in out["error"]
        assert out["indices"] == []


class TestWeatherFormatters:
    def test_brief(self):
        from app.services.weather_service import format_weather_brief

        assert format_weather_brief({}) == ""
        assert format_weather_brief({"error": "x"}) == ""
        out = format_weather_brief({"location": "上海市", "weather": "多云",
                                    "temperature": "25", "humidity": "60"})
        assert out == "上海市 多云 25°C 湿度60%"

    def test_detail(self):
        from app.services.weather_service import format_weather_detail

        assert format_weather_detail(None) == ""
        assert format_weather_detail({"error": "x"}) == ""
        out = format_weather_detail({
            "location": "上海市", "weather": "多云", "temperature": "25",
            "feels_like": "24", "humidity": "60", "wind_dir": "东南风",
            "wind_scale": "3",
        })
        assert "当前天气：上海市 多云 25°C" in out
        assert "(体感 24°C)" in out
        assert "东南风 3级" in out


# ---------------------------------------------------------------------------
# rule_service
# ---------------------------------------------------------------------------

def _rule_providers(svc):
    """给 RuleService 注入 HA catalog/devices/services 三类 provider。"""
    svc.set_ha_catalog_provider(lambda: (
        "- light.l1 (类型:light, 状态:on) 名称:客厅灯\n"
        "- switch.plug (类型:switch, 状态:off) 名称:插座"
    ))
    svc.set_ha_devices_provider(AsyncMock(return_value=[
        {"entity_id": "light.l1", "name": "客厅灯", "domain": "light",
         "state": "on", "attributes": {"friendly_name": "客厅灯"}},
        {"entity_id": "switch.plug", "name": "插座", "domain": "switch",
         "state": "off", "attributes": {"friendly_name": "插座"}},
    ]))
    svc.set_ha_services_provider(AsyncMock(return_value={
        "light": {"turn_on": [], "turn_off": []},
        "switch": {"turn_on": [], "turn_off": []},
    }))


def _valid_action(entity_id="light.l1", domain="light", service="turn_on"):
    return {
        "mcp_tool_name": "ha_devices___call_service",
        "mcp_tool_input": {"domain": domain, "service": service,
                           "entity_id": entity_id, "data": {}},
    }


def _rule_json(action=None, **extra):
    payload = {
        "name": "开灯规则", "condition": "有人", "type": "vision",
        "actions": [action or _valid_action()],
        "action_descriptions": ["开客厅灯"], "cooldown_seconds": 5,
        "summary": "有人就开灯",
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


@contextmanager
def _rule_env(enabled=True, chat_side_effect=None):
    """RuleService + mock client + Database(prefs) + per-user 无配置。"""
    from app.services.rule_service import RuleService

    client = MagicMock()
    client.enabled = enabled
    client.chat = AsyncMock(side_effect=chat_side_effect)
    svc = RuleService(client=client)
    _rule_providers(svc)
    notes_db = MagicMock()
    notes_db.prefs_get_by_scope = AsyncMock(return_value={})
    db_mock = MagicMock()
    db_mock.get.return_value = notes_db
    with patch("app.core.database.Database", db_mock), \
         patch("app.core.key_resolver.resolve_key_for_role_user",
               new=AsyncMock(return_value=None)):
        yield svc, client


class TestRuleServiceBuild:
    def test_filter_devices_and_catalog_parse(self):
        from app.services.rule_service import RuleService, _filter_devices

        devices = [{"name": "灯", "entity_id": "light.l1"}]
        assert _filter_devices("", devices) is devices  # 空 query → 全量
        assert _filter_devices("x", []) == []
        assert _filter_devices("灯", devices) == devices
        svc = RuleService()
        assert svc._parse_ha_catalog("- light.l1 (类型:light, 状态:on) 名称:客厅灯") == [
            {"entity_id": "light.l1", "domain": "light", "name": "客厅灯"}
        ]

    async def test_build_rule_success(self):
        with _rule_env(chat_side_effect=[_rule_json()]) as (svc, client):
            out = await svc.build_rule("有人就开客厅灯", camera_id="cam-1")
        assert out["condition"] == "有人"
        assert out["camera_id"] == "cam-1"
        assert out["type"] == "vision"
        client.chat.assert_awaited_once()

    async def test_build_rule_type_normalized(self):
        """LLM 输出非法 type → 归一化为 vision。"""
        with _rule_env(chat_side_effect=[_rule_json(type="weird")]) as (svc, _):
            out = await svc.build_rule("x")
        assert out["type"] == "vision"

    async def test_build_rule_parse_retry_then_success(self):
        with _rule_env(chat_side_effect=["不是JSON", _rule_json()]) as (svc, client):
            out = await svc.build_rule("x")
        assert out["condition"] == "有人"
        assert client.chat.await_count == 2

    async def test_build_rule_parse_fail_returns_fallback(self):
        with _rule_env(chat_side_effect=["bad"] * 3) as (svc, _):
            out = await svc.build_rule("有人就开灯", camera_id="cam-2")
        assert out["condition"] == ""
        assert out["camera_id"] == "cam-2"  # 兜底规则保留摄像头绑定
        assert out["summary"] == "有人就开灯"

    async def test_build_rule_validation_retry_then_success(self):
        bad = _rule_json(actions=[
            _valid_action(),
            {"mcp_tool_name": "wrong",
             "mcp_tool_input": {"domain": "", "service": "", "entity_id": "", "data": {}}},
        ])
        with _rule_env(chat_side_effect=[bad, _rule_json()]) as (svc, client):
            out = await svc.build_rule("x")
        assert out["actions"][0]["mcp_tool_input"]["entity_id"] == "light.l1"
        assert client.chat.await_count == 2

    async def test_build_rule_validation_exhausted_returns_parsed(self):
        bad = _rule_json(actions=[{"mcp_tool_name": "wrong",
                                   "mcp_tool_input": {"domain": "", "service": "",
                                                      "entity_id": "", "data": {}}}])
        with _rule_env(chat_side_effect=[bad] * 3) as (svc, _):
            out = await svc.build_rule("x")
        assert out["actions"][0]["mcp_tool_name"] == "wrong"  # 重试耗尽仍返回解析结果

    async def test_build_rule_llm_disabled_fallback(self):
        with _rule_env(enabled=False) as (svc, client):
            out = await svc.build_rule("有人就开灯")
        assert out["condition"] == ""
        client.chat.assert_not_awaited()

    async def test_prepare_context_provider_failures_swallowed(self):
        """三个 HA provider 抛异常 → 降级继续出 prompt，不崩。"""
        with _rule_env() as (svc, client):
            svc.set_ha_catalog_provider(MagicMock(side_effect=RuntimeError("catalog down")))
            svc.set_ha_devices_provider(AsyncMock(side_effect=RuntimeError("devices down")))
            svc.set_ha_services_provider(AsyncMock(side_effect=RuntimeError("services down")))
            ctx = await svc._prepare_rule_context("灯")
        assert ctx["client"] is client
        assert ctx["full_devices"] == []
        assert ctx["services_info"] == {}
        assert "(暂无可用设备)" in ctx["system_prompt"]

    async def test_prepare_context_notes_db_failure_swallowed(self):
        """读取用户实体备注的 DB 异常 → 吞掉继续（controls 逻辑跳过也能出 prompt）。"""
        from app.services.rule_service import RuleService

        client = MagicMock()
        client.enabled = True
        client.chat = AsyncMock()
        svc = RuleService(client=client)
        _rule_providers(svc)
        db_mock = MagicMock()
        db_mock.get = MagicMock(side_effect=RuntimeError("db closed"))
        with patch("app.core.database.Database", db_mock), \
             patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=None)):
            ctx = await svc._prepare_rule_context("灯")
        assert ctx["client"] is client  # 流程没崩，照常返回上下文


class TestRuleServiceRevise:
    @staticmethod
    def _current():
        return {"id": "r1", "name": "开灯规则", "condition": "有人",
                "type": "vision", "actions": [], "action_descriptions": [],
                "cooldown_seconds": 5, "summary": "s"}

    async def test_revise_success_with_change_summary(self):
        new_json = json.dumps({
            "change_summary": "把灯改成插座",
            "name": "开插座", "condition": "有人", "type": "vision",
            "actions": [_valid_action(entity_id="switch.plug", domain="switch")],
        }, ensure_ascii=False)
        with _rule_env(chat_side_effect=[new_json]) as (svc, _):
            out = await svc.revise_rule(self._current(), "把灯改成插座")
        assert out["summary"] == "把灯改成插座"
        assert out["rule"]["actions"][0]["mcp_tool_input"]["entity_id"] == "switch.plug"

    async def test_revise_llm_disabled_fallback(self):
        with _rule_env(enabled=False) as (svc, _):
            out = await svc.revise_rule(self._current(), "改条件")
        assert out["fallback"] is True
        assert out["rule"]["name"] == "开灯规则"

    async def test_revise_parse_retry_then_success(self):
        with _rule_env(chat_side_effect=["nope", _rule_json()]) as (svc, client):
            out = await svc.revise_rule(self._current(), "改条件")
        assert out["rule"]["condition"] == "有人"
        assert client.chat.await_count == 2

    async def test_revise_parse_fail_returns_fallback(self):
        with _rule_env(chat_side_effect=["bad"] * 3) as (svc, _):
            out = await svc.revise_rule(self._current(), "改条件")
        assert out["fallback"] is True
        assert "修改失败" in out["summary"]

    async def test_revise_validation_exhausted_returns_parsed(self):
        bad = json.dumps({"name": "n", "actions": [{"mcp_tool_name": "wrong",
                                                    "mcp_tool_input": {"domain": "", "service": "",
                                                                       "entity_id": "", "data": {}}}]},
                         ensure_ascii=False)
        with _rule_env(chat_side_effect=[bad] * 3) as (svc, _):
            out = await svc.revise_rule(self._current(), "改条件")
        assert out["rule"]["actions"][0]["mcp_tool_name"] == "wrong"

    async def test_revise_keeps_original_type_when_missing(self):
        """新 JSON 不带 type → 保留原规则 type（不兜底 vision）。"""
        new_json = json.dumps({"condition": "天黑", "actions": []}, ensure_ascii=False)
        with _rule_env(chat_side_effect=[new_json]) as (svc, _):
            out = await svc.revise_rule(self._current(), "改条件")
        assert out["rule"]["type"] == "vision"

    async def test_revise_empty_change_summary_uses_default(self):
        new_json = json.dumps({"change_summary": "", "condition": "x", "actions": []},
                              ensure_ascii=False)
        with _rule_env(chat_side_effect=[new_json]) as (svc, _):
            out = await svc.revise_rule(self._current(), "改条件")
        assert out["summary"] == "已更新"


class TestRuleServiceExplain:
    async def test_explain_success(self):
        with _rule_env(chat_side_effect=["这条规则在工作日 8 点开灯。"]) as (svc, client):
            out = await svc.explain_rule({"name": "n", "condition": "c"}, "什么时候触发？")
        assert "工作日" in out
        client.chat.assert_awaited_once()

    async def test_explain_disabled(self):
        with _rule_env(enabled=False) as (svc, _):
            out = await svc.explain_rule({"name": "n"}, "q")
        assert out == "LLM 未配置，无法解释规则。"

    async def test_explain_empty_content(self):
        with _rule_env(chat_side_effect=[""]) as (svc, _):
            assert await svc.explain_rule({"name": "n"}, "q") == "无法生成解释。"

    async def test_explain_llm_error(self):
        with _rule_env(chat_side_effect=[RuntimeError("timeout")]) as (svc, _):
            out = await svc.explain_rule({"name": "n"}, "q")
        assert out.startswith("解释失败")


class TestValidateActions:
    @staticmethod
    def _svc():
        from app.services.rule_service import RuleService

        return RuleService()

    def test_multiple_errors_reported(self):
        action = {
            "mcp_tool_name": "other_tool",  # 工具名错误
            "mcp_tool_input": {"domain": "light", "service": "turn_on",
                               "entity_id": "light.l1",
                               "data": {"effect": "rainbow", "brightness": 10}},
        }
        devices = [{"entity_id": "light.l1", "domain": "light",
                    "attributes": {"effects": ["white"]},
                    "_controls": {"brightness": {"param": "brightness"}}}]
        services_info = {"light": {"turn_on": ["effect", "brightness"], "turn_off": []}}
        errors = self._svc()._validate_actions([action], devices, services_info)
        joined = "\n".join(errors)
        assert "mcp_tool_name 必须是" in joined
        # 枚举校验：effect=rainbow 不在 attributes.effects=["white"]
        assert "rainbow" in joined
        assert len(errors) == 2

    def test_missing_fields_and_unknown_entity(self):
        action = {"mcp_tool_name": "ha_devices___call_service",
                  "mcp_tool_input": {"domain": "", "service": "",
                                     "entity_id": "light.nope", "data": None}}
        devices = [{"entity_id": "light.l1", "domain": "light", "attributes": {}}]
        errors = self._svc()._validate_actions([action], devices,
                                               {"light": {"turn_on": []}})
        joined = "\n".join(errors)
        assert "缺少 domain" in joined
        assert "缺少 service" in joined
        assert "light.nope" in joined and "不存在" in joined

    def test_unknown_domain_and_service(self):
        action = {"mcp_tool_name": "ha_devices___call_service",
                  "mcp_tool_input": {"domain": "media_player", "service": "play",
                                     "entity_id": "light.l1", "data": {}}}
        errors = self._svc()._validate_actions([action],
                                               [{"entity_id": "light.l1", "attributes": {}}],
                                               {"light": {"turn_on": []}})
        joined = "\n".join(errors)
        assert "media_player" in joined and "不存在" in joined
        assert "service 'play'" in joined

    def test_unknown_data_field(self):
        action = {"mcp_tool_name": "ha_devices___call_service",
                  "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                     "entity_id": "light.l1",
                                     "data": {"fancy": 1}}}
        errors = self._svc()._validate_actions([action],
                                               [{"entity_id": "light.l1", "attributes": {}}],
                                               {"light": {"turn_on": ["brightness"]}})
        assert any("fancy" in e for e in errors)

    def test_service_device_mismatch(self):
        """service 需要的参数与设备可控参数无交集 → 报不匹配。"""
        action = {"mcp_tool_name": "ha_devices___call_service",
                  "mcp_tool_input": {"domain": "light", "service": "turn_on",
                                     "entity_id": "light.l1", "data": {}}}
        devices = [{"entity_id": "light.l1", "attributes": {},
                    "_controls": {"brightness": {"param": "brightness"}}}]
        errors = self._svc()._validate_actions([action], devices,
                                               {"light": {"turn_on": ["effect"]}})
        joined = "\n".join(errors)
        assert "不匹配" in joined
        assert "brightness" in joined

    def test_parse_json_invalid(self):
        assert self._svc()._parse_json("完全不是 JSON") == {}


# ---------------------------------------------------------------------------
# scene_service
# ---------------------------------------------------------------------------

class TestSceneServiceGaps:
    async def test_set_ha_and_crud(self, temp_db):
        from app.services.scene_service import SceneService

        svc = SceneService()
        ha = MagicMock()
        ha.call_service = AsyncMock()
        ha_service = MagicMock()
        ha_service.get_all_devices = AsyncMock(return_value=[])
        svc.set_ha(ha, ha_service)
        assert svc._ha_client_ref[0] is ha
        assert svc._ha_service_ref[0] is ha_service

        assert await svc.list_scenes() == []
        with pytest.raises(ValueError, match="场景名不能为空"):
            await svc.create_scene("  ", [{"domain": "light"}])
        with pytest.raises(ValueError, match="至少需要一个动作"):
            await svc.create_scene("空场景", [{"bad": "shape"}, "junk"])

        created = await svc.create_scene("观影", [
            {"domain": "light", "service": "turn_on", "entity_id": "light.l1",
             "data": {"brightness": 100}},
            "垃圾条目",  # 非法条目被过滤
            {"domain": "", "service": "", "entity_id": "x"},
        ])
        assert created["name"] == "观影"
        assert len(created["actions"]) == 1
        # data 非 dict 归一为 {}
        created2 = await svc.create_scene("默认", [
            {"domain": "light", "service": "turn_on", "entity_id": "light.l2",
             "data": "not-a-dict"},
        ])
        assert created2["actions"][0]["data"] == {}

        assert await svc.delete_scene(created["id"]) is True
        assert await svc.delete_scene(created["id"]) is False

    async def test_apply_scene_not_found_and_no_ha(self, temp_db):
        from app.services.scene_service import SceneService

        svc = SceneService()
        with pytest.raises(ValueError, match="场景不存在"):
            await svc.apply_scene("missing")
        scene = await svc.create_scene("s1", [
            {"domain": "light", "service": "turn_on", "entity_id": "light.l1", "data": {}}
        ])
        with pytest.raises(RuntimeError, match="HA 服务不可用"):
            await svc.apply_scene(scene["id"])

    async def test_apply_scene_partial_failure(self, temp_db):
        from app.services.scene_service import SceneService

        svc = SceneService()
        scene = await svc.create_scene("混合", [
            {"domain": "light", "service": "turn_on", "entity_id": "light.ok", "data": {}},
            {"domain": "light", "service": "turn_on", "entity_id": "light.bad", "data": {}},
        ])
        ha = MagicMock()

        async def call_service(domain, service, entity_id, data):
            if entity_id == "light.bad":
                raise RuntimeError("device offline")

        ha.call_service = call_service
        svc.set_ha(ha, MagicMock())
        out = await svc.apply_scene(scene["id"])
        assert out == {"scene": "混合", "total": 2, "ok": 1,
                       "results": [{"entity_id": "light.ok", "ok": True},
                                   {"entity_id": "light.bad", "ok": False,
                                    "error": "device offline"}]}

    async def test_capture_no_ha_and_empty(self, temp_db):
        from app.services.scene_service import SceneService

        svc = SceneService()
        with pytest.raises(RuntimeError, match="HA 服务不可用"):
            await svc.capture_scene("当前")
        ha_service = MagicMock()
        ha_service.get_all_devices = AsyncMock(return_value=[
            {"entity_id": "sensor.temp", "state": "23"},   # domain 不可捕获
        ])
        svc.set_ha(MagicMock(), ha_service)
        with pytest.raises(ValueError, match="没有可捕获的设备状态"):
            await svc.capture_scene("当前")

    async def test_capture_cover_climate_and_light_attrs(self, temp_db):
        """cover open → set_cover_position；climate → temperature；light 亮度/色温。"""
        from app.services.scene_service import SceneService

        svc = SceneService()
        ha_service = MagicMock()
        ha_service.get_all_devices = AsyncMock(return_value=[
            {"entity_id": "cover.win", "state": "open",
             "attributes": {"current_position": 60}},
            {"entity_id": "climate.ac", "state": "cool",
             "attributes": {"temperature": 26}},
            {"entity_id": "light.l1", "state": "on",
             "attributes": {"brightness": 200, "color_temp": 350}},
            {"entity_id": "switch.plug", "state": "off", "attributes": {}},
        ])
        svc.set_ha(MagicMock(), ha_service)
        scene = await svc.capture_scene("当前")
        acts = {a["entity_id"]: a for a in scene["actions"]}
        assert acts["cover.win"]["service"] == "set_cover_position"
        assert acts["cover.win"]["data"] == {"position": 60}
        assert acts["climate.ac"]["service"] == "turn_on"
        assert acts["climate.ac"]["data"] == {"temperature": 26}
        assert acts["light.l1"]["data"] == {"brightness": 200, "color_temp_kelvin": 350}
        assert acts["switch.plug"]["service"] == "turn_off"

    async def test_capture_cover_closed_and_climate_off(self, temp_db):
        from app.services.scene_service import SceneService

        svc = SceneService()
        ha_service = MagicMock()
        ha_service.get_all_devices = AsyncMock(return_value=[
            {"entity_id": "cover.win", "state": "closed", "attributes": {}},
            {"entity_id": "climate.ac", "state": "off", "attributes": {}},
        ])
        svc.set_ha(MagicMock(), ha_service)
        scene = await svc.capture_scene("离家")
        acts = {a["entity_id"]: a for a in scene["actions"]}
        assert acts["cover.win"] == {"domain": "cover", "service": "close_cover",
                                     "entity_id": "cover.win", "data": {}}
        assert acts["climate.ac"]["service"] == "turn_off"
        assert acts["climate.ac"]["data"] == {}

    async def test_state_to_action_data_none_for_unknown_domain(self):
        from app.services.scene_service import SceneService

        out = SceneService._current_state_to_action_data(
            {"entity_id": "sensor.x", "state": "23"})
        assert out is None


# ---------------------------------------------------------------------------
# scheduler_service
# ---------------------------------------------------------------------------

_SCHED_UNSET = object()


def _make_sched(llm_reply: str = "该下班了", *, llm_client=_SCHED_UNSET):
    from app.services.scheduler_service import SchedulerService

    db = MagicMock()
    db.scheduled_tasks_all = AsyncMock(return_value=[])
    db.scheduled_task_insert = AsyncMock()
    db.scheduled_task_update = AsyncMock()
    db.scheduled_task_delete = AsyncMock()

    tool_executor = MagicMock()
    tool_executor.resolve_tool_name = MagicMock(side_effect=lambda n: n)
    tool_executor.execute_tool_by_name = AsyncMock(
        return_value={"success": True, "result": "ok"})

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value=[])
    session_store = MagicMock()
    session_store.list_summaries = AsyncMock(return_value=[{"id": "sess-1"}])

    if llm_client is _SCHED_UNSET:
        llm_client = MagicMock()
        llm_client.chat = AsyncMock(return_value=llm_reply)

    svc = SchedulerService(db=db, tool_executor=tool_executor,
                           dispatcher_ref=[dispatcher], session_store=session_store,
                           llm_chat_client=llm_client)
    return svc, db, tool_executor, dispatcher, session_store


def _session_state():
    from app.services.session_store import SessionState

    return SessionState(session_id="sess-1", request_id="r1")


class TestComputeNextRunGaps:
    def test_at_empty_and_invalid(self):
        from app.services.scheduler_service import compute_next_run

        assert compute_next_run({"kind": "at", "at": ""}, time.time()) is None
        assert compute_next_run({"kind": "at", "at": "not-a-date"}, time.time()) is None
        assert compute_next_run({"kind": "at", "at": 12345}, time.time()) is None

    def test_cron_empty_expr(self):
        from app.services.scheduler_service import compute_next_run

        assert compute_next_run({"kind": "cron", "expr": ""}, time.time()) is None

    def test_summarize_unknown(self):
        from app.services.scheduler_service import summarize_schedule

        assert summarize_schedule({"kind": "weird"}) == "未知触发"


class TestSchedulerLifecycle:
    async def test_start_stop_tick_loop(self):
        svc, *_ = _make_sched()
        with patch("app.services.scheduler_service._TICK_INTERVAL_SECONDS", 0.01):
            await svc.start()
            assert svc._running is True
            assert svc._tick_task is not None
            await asyncio.sleep(0.05)
            await svc.stop()
        assert svc._running is False
        assert svc._tick_task is None

    async def test_tick_loop_survives_tick_exception(self):
        svc, *_ = _make_sched()
        svc._tick_once = AsyncMock(side_effect=RuntimeError("scan failed"))
        with patch("app.services.scheduler_service._TICK_INTERVAL_SECONDS", 0.01):
            await svc.start()
            await asyncio.sleep(0.05)
            await svc.stop()
        assert svc._tick_once.await_count >= 2  # 异常后循环继续

    async def test_stop_cancels_mid_tick(self):
        """stop 打断正在执行的 _tick_once → CancelledError 在 _tick_loop 中透传。"""
        svc, *_ = _make_sched()

        async def _slow_tick():
            await asyncio.sleep(5)

        svc._tick_once = _slow_tick
        with patch("app.services.scheduler_service._TICK_INTERVAL_SECONDS", 0.01):
            await svc.start()
            await asyncio.sleep(0.05)  # 让 tick 进入 slow tick
            await asyncio.wait_for(svc.stop(), timeout=2)
        assert svc._tick_task is None

    async def test_load_tasks_marks_interrupted_and_expired(self):
        svc, db, *_ = _make_sched()
        db.scheduled_tasks_all = AsyncMock(return_value=[
            {"id": "t-running", "name": "r", "enabled": True,
             "schedule": {"kind": "every", "every_seconds": 60},
             "payload": {"kind": "tool"}, "last_status": "running"},
            {"id": "t-expired", "name": "e", "enabled": True,
             "schedule": {"kind": "at", "at": "2020-01-01T00:00:00"},
             "payload": {"kind": "tool"}, "last_status": ""},
            {"id": "t-disabled", "name": "d", "enabled": False,
             "schedule": {"kind": "every", "every_seconds": 60},
             "payload": {"kind": "tool"}, "last_status": ""},
        ])
        await svc._load_tasks()
        assert svc._tasks["t-running"]["last_status"] == "interrupted"
        assert "进程重启" in svc._tasks["t-running"]["last_error"]
        assert svc._tasks["t-expired"]["enabled"] is False
        assert svc._tasks["t-expired"]["last_status"] == "expired"
        assert "停机期间" in svc._tasks["t-expired"]["last_error"]
        assert svc._tasks["t-disabled"]["next_run_at"] is None
        assert db.scheduled_task_update.await_count == 3

    async def test_tick_once_executes_due_task(self):
        svc, db, tool_exec, _, _ = _make_sched()
        task = await svc.add_task({
            "name": "开灯", "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "tool", "tool_name": "light.turn_on",
                        "tool_input": {"entity_id": "light.l1"}},
            "user_id": "u1",
        })
        task["next_run_at"] = time.time() - 1  # 已到期
        await svc._tick_once()
        await asyncio.sleep(0.01)  # 让后台执行任务开跑
        tool_exec.execute_tool_by_name.assert_awaited_once()
        # 等后台执行完成后状态收敛
        for _ in range(100):
            if task.get("last_status") == "success":
                break
            await asyncio.sleep(0.01)
        assert task["last_status"] == "success"

    async def test_tick_once_skips_executing_and_uses_task_manager(self):
        svc, db, tool_exec, _, _ = _make_sched()
        spawned: list = []
        spawned_tasks: list = []
        tm = MagicMock()

        def _spawn(coro, name=None):
            spawned.append(name)
            t = asyncio.create_task(coro)
            spawned_tasks.append(t)
            return t

        tm.spawn = _spawn
        svc._task_manager = tm
        task = await svc.add_task({
            "name": "t", "schedule": {"kind": "every", "every_seconds": 60},
            "payload": {"kind": "tool", "tool_name": "x"},
        })
        task["next_run_at"] = time.time() - 1
        svc._executing.add(task["id"])
        await svc._tick_once()  # 在执行中 → 跳过
        assert spawned == []
        svc._executing.clear()
        await svc._tick_once()  # 有 task_manager → spawn 路径
        assert spawned == [f"scheduled-{task['id']}"]
        await asyncio.gather(*spawned_tasks)
        assert task["last_status"] == "success"


class TestSchedulerExecuteGaps:
    async def _task(self, svc, payload):
        return await svc.add_task({"name": "x",
                                   "schedule": {"kind": "every", "every_seconds": 60},
                                   "payload": payload})

    async def test_unknown_payload_kind_fails(self):
        svc, *_ = _make_sched()
        task = await self._task(svc, {"kind": "weird"})
        await svc._execute_task(task)
        assert task["last_status"] == "failed"
        assert "unknown payload kind" in task["last_error"]

    async def test_tool_payload_missing_name(self):
        svc, *_ = _make_sched()
        task = await self._task(svc, {"kind": "tool", "tool_name": ""})
        await svc._execute_task(task)
        assert "tool_name is required" in task["last_error"]

    async def test_tool_payload_failure_result_raises(self):
        svc, _, tool_exec, _, _ = _make_sched()
        tool_exec.execute_tool_by_name = AsyncMock(
            return_value={"success": False, "error": "boom"})
        task = await self._task(svc, {"kind": "tool", "tool_name": "x"})
        await svc._execute_task(task)
        assert task["last_status"] == "failed"
        assert "returned failure" in task["last_error"]

    async def test_message_payload_empty_message(self):
        svc, *_ = _make_sched()
        task = await self._task(svc, {"kind": "message", "message": "  "})
        await svc._execute_task(task)
        assert "payload.message is required" in task["last_error"]

    async def test_message_payload_no_dispatcher(self):
        svc, *_ = _make_sched()
        svc._dispatcher_ref[0] = None
        task = await self._task(svc, {"kind": "message", "message": "hi"})
        task["user_id"] = "u1"
        await svc._execute_task(task)
        assert "dispatcher not available" in task["last_error"]

    async def test_reminder_missing_source(self):
        svc, *_ = _make_sched()
        task = await self._task(svc, {"kind": "reminder"})
        await svc._execute_task(task)
        assert "至少填一个" in task["last_error"]

    async def test_reminder_no_client(self):
        svc, *_ = _make_sched(llm_client=None)
        task = await self._task(svc, {"kind": "reminder", "intent": "下班"})
        task["user_id"] = "u1"
        with patch("app.services.scheduler_service.build_per_user_chat_client",
                   new=AsyncMock(return_value=None)):
            await svc._execute_task(task)
        assert "无可用的 LLM 客户端" in task["last_error"]

    async def test_reminder_empty_llm_reply(self):
        svc, _, _, _, session_store = _make_sched(llm_reply="   ")
        session_store.get_or_create = AsyncMock(return_value=_session_state())
        session_store.store_session = AsyncMock()
        task = await self._task(svc, {"kind": "reminder", "intent": "下班"})
        task["user_id"] = "u1"
        await svc._execute_task(task)
        assert "LLM 返回空回复" in task["last_error"]

    async def test_record_event_failure_swallowed(self):
        from app.services import alert_service as alert_mod

        svc, *_ = _make_sched()
        task = await self._task(svc, {"kind": "tool", "tool_name": "x"})
        with patch.object(alert_mod.alert_service, "record",
                          new=AsyncMock(side_effect=RuntimeError("db down"))):
            await svc._execute_task(task)
        assert task["last_status"] == "success"  # 记录失败不影响任务

    async def test_message_push_reply_ws_failure_ok(self):
        """message 任务：从 ToastStream 指令抽取回复；WS 推送失败不影响任务成功。"""
        from app.schema.chat_schema import Instruction, Template
        from app.core import ws_registry

        svc, _, _, dispatcher, session_store = _make_sched()
        inst = Instruction.build_instruction(
            Template.ToastStream(stream="好的"), "rid", "sess-1")
        dispatcher.dispatch = AsyncMock(return_value=[inst])
        task = await self._task(svc, {"kind": "message", "message": "hi"})
        task["user_id"] = "u1"
        with patch.object(ws_registry, "push_to_user",
                          new=AsyncMock(side_effect=RuntimeError("ws closed"))):
            await svc._execute_task(task)
        assert task["last_status"] == "success"
        assert task["last_reply"] == "好的"
        dispatcher.dispatch.assert_awaited_once()


class TestSchedulerCrudGaps:
    async def test_update_missing_and_meta_skip(self):
        svc, db, *_ = _make_sched()
        assert await svc.update_task("nope", {"enabled": True}) is None
        task = await svc.add_task({"name": "t", "id": "orig-id",
                                   "schedule": {"kind": "every", "every_seconds": 60},
                                   "payload": {"kind": "tool"}})
        created = task["created_at"]
        out = await svc.update_task("orig-id", {"id": "hacked", "created_at": 1,
                                                "name": "renamed", "enabled": False})
        assert out["id"] == "orig-id"  # id/created_at 不可被 patch
        assert out["created_at"] == created
        assert out["name"] == "renamed"
        assert out["next_run_at"] is None  # enabled=False → 不再调度

    async def test_delete_task_returns_existence(self):
        svc, db, *_ = _make_sched()
        await svc.add_task({"name": "t", "schedule": {"kind": "every", "every_seconds": 60},
                            "payload": {}})
        tasks = await svc.list_tasks()
        tid = tasks[0]["id"]
        assert await svc.delete_task(tid) is True
        assert await svc.delete_task(tid) is False
        db.scheduled_task_delete.assert_awaited_with(tid)


class TestSchedulerRunNowGaps:
    @staticmethod
    async def _add(svc):
        return await svc.add_task({"name": "t",
                                   "schedule": {"kind": "every", "every_seconds": 60},
                                   "payload": {"kind": "tool", "tool_name": "x"}})

    async def test_run_now_missing_task(self):
        svc, *_ = _make_sched()
        assert await svc.run_now("nope", wait=True) is None

    async def test_run_now_skips_if_executing(self):
        svc, *_ = _make_sched()
        task = await self._add(svc)
        svc._executing.add(task["id"])
        assert await svc.run_now(task["id"], wait=True) is task

    async def test_run_now_wait_timeout(self):
        svc, _, tool_exec, _, _ = _make_sched()

        async def _slow(*a, **k):
            await asyncio.sleep(0.4)
            return {"success": True, "result": "slow-ok"}

        tool_exec.execute_tool_by_name = AsyncMock(side_effect=_slow)
        task = await self._add(svc)
        out = await svc.run_now(task["id"], wait=True, timeout=0.05)
        assert out is task
        # shield 的后台执行继续跑完
        for _ in range(100):
            if task.get("last_status") == "success":
                break
            await asyncio.sleep(0.01)
        assert task["last_status"] == "success"

    async def test_run_now_wait_swallows_execute_exception(self):
        svc, db, *_ = _make_sched()
        db.scheduled_task_update = AsyncMock(side_effect=RuntimeError("db down"))
        task = await self._add(svc)
        out = await svc.run_now(task["id"], wait=True)  # 不应向外抛
        assert out is task

    async def test_run_now_no_wait_with_task_manager(self):
        svc, _, _, _, _ = _make_sched()
        spawned: list = []
        spawned_tasks: list = []
        tm = MagicMock()

        def _spawn(coro, name=None):
            spawned.append(name)
            t = asyncio.create_task(coro)
            spawned_tasks.append(t)
            return t

        tm.spawn = _spawn
        svc._task_manager = tm
        task = await self._add(svc)
        out = await svc.run_now(task["id"], wait=False)
        assert out is task
        assert spawned == [f"scheduled-manual-{task['id']}"]
        await asyncio.gather(*spawned_tasks)
        assert task["last_status"] == "success"

    async def test_run_now_no_wait_background_fallback(self):
        svc, *_ = _make_sched()
        task = await self._add(svc)
        out = await svc.run_now(task["id"], wait=False)
        assert out is task
        pending = list(svc._background_tasks)
        assert pending
        await asyncio.gather(*pending)
        assert task["last_status"] == "success"


# ---------------------------------------------------------------------------
# stt_service
# ---------------------------------------------------------------------------

class TestSttResolveConfig:
    async def test_per_user_key_preferred(self):
        from app.services import stt_service

        key = {"api_key": "sk-user", "base_url": "https://u.example.com/v1/",
               "model": "m-user"}
        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value=key)):
            cfg = await stt_service._resolve_config("u1")
        assert cfg["available"] is True
        assert cfg["api_key"] == "sk-user"
        assert cfg["base_url"] == "https://u.example.com/v1"  # 尾斜杠被去掉
        assert cfg["model"] == "m-user"

    async def test_per_user_key_without_api_key_falls_back(self):
        from app.services import stt_service

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(return_value={"api_key": ""})), \
             patch("app.services.stt_service.resolve_key_for_role",
                   return_value={"api_key": "sk-global",
                                 "base_url": "https://g.example.com/v1",
                                 "model": "m-global"}):
            cfg = await stt_service._resolve_config("u1")
        assert cfg["api_key"] == "sk-global"

    async def test_per_user_resolver_raises_falls_back(self):
        from app.services import stt_service

        with patch("app.core.key_resolver.resolve_key_for_role_user",
                   new=AsyncMock(side_effect=RuntimeError("db broken"))), \
             patch("app.services.stt_service.resolve_key_for_role",
                   return_value=None):
            cfg = await stt_service._resolve_config("u1")
        assert cfg == {"available": False, "timeout": 30.0}


# ---------------------------------------------------------------------------
# schedule_parser_service
# ---------------------------------------------------------------------------

class _FakeAsyncHttp:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _FakeSyncHttp:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@contextmanager
def _parser_env(content):
    """patch 掉 schedule_parser 的 LLM 构造；content 为模型回复文本。

    Yields (fake_llm, fake_sync, fake_async)。
    """
    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(return_value=MagicMock(content=content))
    fake_module = MagicMock()
    fake_module.ChatOpenAI = MagicMock(return_value=fake_llm)
    sync_c, async_c = _FakeSyncHttp(), _FakeAsyncHttp()
    import sys

    with patch.dict(sys.modules, {"langchain_openai": fake_module}), \
         patch("app.services.schedule_parser_service.new_sync_client", return_value=sync_c), \
         patch("app.services.schedule_parser_service.new_client", return_value=async_c), \
         patch("app.services.schedule_parser_service._load_model_config_from_config",
               return_value={"model": "test-model", "base_url": "http://x/v1",
                             "api_key": "k"}):
        yield fake_llm, sync_c, async_c


class TestParseSchedule:
    async def test_success_every(self):
        from app.services import schedule_parser_service as sps

        with _parser_env('{"kind": "every", "every_seconds": 90}') as (llm, sync_c, async_c):
            out = await sps.parse_schedule("每90秒")
        assert out["schedule"] == {"kind": "every", "every_seconds": 90}
        assert out["summary"] == "每 1.5 分钟"
        assert async_c.closed and sync_c.closed  # 连接用完即关
        # system prompt（tuple 首个消息）包含翻译规则与当前时间占位替换
        system_msg = llm.ainvoke.await_args.args[0][0]
        assert system_msg[0] == "system"
        assert "时间表达翻译器" in system_msg[1]

    async def test_llm_error_key_raises(self):
        from app.services import schedule_parser_service as sps

        # error JSON（str content）→ ValueError 带原文
        with _parser_env('{"error": "时间描述不明确"}') as (llm, sync_c, async_c):
            with pytest.raises(ValueError, match="时间描述不明确"):
                await sps.parse_schedule("大概吧")
        assert async_c.closed and sync_c.closed  # finally 分支仍关连接

    async def test_non_str_content_covers_str_branch(self):
        from app.services import schedule_parser_service as sps

        # 非 str content → str(dict) 是单引号伪 JSON → 解析失败抛 ValueError
        with _parser_env("") as (llm, sync_c, async_c):
            llm.ainvoke = AsyncMock(return_value=MagicMock(content={"error": "x"}))
            with pytest.raises(ValueError):
                await sps.parse_schedule("大概吧")
        assert async_c.closed and sync_c.closed

    async def test_invalid_schedule_raises(self):
        from app.services import schedule_parser_service as sps

        with _parser_env('{"kind": "at"}') as (llm, *_):
            with pytest.raises(ValueError, match="at 触发缺少"):
                await sps.parse_schedule("明天")

    async def test_ainvoke_failure_propagates(self):
        from app.services import schedule_parser_service as sps

        with _parser_env("") as (llm, sync_c, async_c):
            llm.ainvoke = AsyncMock(side_effect=RuntimeError("llm down"))
            with pytest.raises(RuntimeError, match="llm down"):
                await sps.parse_schedule("每天8点")
        assert async_c.closed and sync_c.closed

    async def test_empty_phrase(self):
        from app.services.schedule_parser_service import parse_schedule

        with pytest.raises(ValueError, match="不能为空"):
            await parse_schedule("   ")

    async def test_connection_close_failures_swallowed(self):
        """finally 中关闭 http 客户端失败 → 吞掉，不影响返回/异常传播。"""
        from app.services import schedule_parser_service as sps

        fake_module = MagicMock()
        fake_llm = MagicMock()
        fake_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content='{"kind": "every", "every_seconds": 60}'))
        fake_module.ChatOpenAI = MagicMock(return_value=fake_llm)
        sync_c, async_c = _FakeSyncHttp(), _FakeAsyncHttp()
        sync_c.close = MagicMock(side_effect=RuntimeError("close boom"))
        async_c.aclose = AsyncMock(side_effect=RuntimeError("aclose boom"))
        import sys

        with patch.dict(sys.modules, {"langchain_openai": fake_module}), \
             patch("app.services.schedule_parser_service.new_sync_client", return_value=sync_c), \
             patch("app.services.schedule_parser_service.new_client", return_value=async_c), \
             patch("app.services.schedule_parser_service._load_model_config_from_config",
                   return_value={"model": "m", "base_url": "http://x/v1", "api_key": "k"}):
            out = await sps.parse_schedule("每60秒")  # 关闭失败不抛
        assert out["schedule"] == {"kind": "every", "every_seconds": 60}
