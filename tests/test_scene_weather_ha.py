"""阶段3：HA Scenes 复用 + 天气降级链（和风优先 → HA weather 兜底）。

覆盖：
- scene_service：list_ha_scenes / apply_ha_scene / capture_ha_scene（中文 slug）/
  resolve_scene（HA 优先、本地兜底、按 id、无匹配）
- scene_routes：/scenes/ha/apply 注册顺序在 /scenes/{scene_id}/apply 之前
- 天气链：和风未配置+家庭位置 → HA 兜底；和风未配置+显式城市 → 报错不兜底；
  和风成功 → source=qweather；和风失败+家庭 → HA；无 HA 实体 → 不可用；
  _beaufort_from_kmh / _HA_CONDITION_ZH 映射；缓存两源共用
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scene_service import SceneService


def _ha_stub(entities: list[dict] | None = None, client: MagicMock | None = None):
    ha = MagicMock()
    ha.get_entities_by_domains = AsyncMock(return_value=entities or [])
    ha._client = client
    return ha


# ===================== SceneService HA 层 =====================

class TestSceneHaLayer:
    async def test_list_ha_scenes(self):
        # get_entities_by_domains 在生产侧已按 domain 过滤，桩数据只给 scene 域
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub([
            {"entity_id": "scene.movie", "name": "观影", "state": "off", "device_class": None},
        ]))
        scenes = await svc.list_ha_scenes()
        assert scenes == [{"entity_id": "scene.movie", "name": "观影",
                           "state": "off", "kind": "ha"}]

    async def test_list_ha_scenes_no_service_returns_empty(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=None)
        assert await svc.list_ha_scenes() == []

    async def test_apply_ha_scene(self):
        client = MagicMock()
        client.call_service = AsyncMock(return_value={})
        svc = SceneService(ha_client=client, ha_service=_ha_stub(client=client))
        result = await svc.apply_ha_scene("scene.movie")
        assert result["via"] == "ha" and result["ok"] == 1
        client.call_service.assert_awaited_once_with("scene", "turn_on", "scene.movie")

    async def test_apply_ha_scene_rejects_non_scene_entity(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub())
        with pytest.raises(ValueError, match="不是 HA 场景实体"):
            await svc.apply_ha_scene("light.bed")

    async def test_capture_ha_scene_chinese_name_slug(self):
        client = MagicMock()
        client.call_service = AsyncMock(return_value={})
        svc = SceneService(ha_client=client, ha_service=_ha_stub(client=client))
        scene = await svc.capture_ha_scene("观影模式")
        assert scene["kind"] == "ha" and scene["name"] == "观影模式"
        # 中文名无 ASCII → 时间戳 slug；英文转小写下划线
        called = client.call_service.await_args
        assert called.args[:2] == ("scene", "create")
        assert called.kwargs.get("data", {}).get("name") == "观影模式"

        scene2 = await svc.capture_ha_scene("Movie Night")
        called2 = client.call_service.await_args
        assert called2.kwargs["data"]["scene_id"] == "movie_night"
        assert scene2["id"] == "scene.movie_night"

    async def test_capture_ha_scene_empty_name(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub())
        with pytest.raises(ValueError, match="场景名不能为空"):
            await svc.capture_ha_scene("  ")


class TestResolveScene:
    async def test_ha_priority_by_name(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub([
            {"entity_id": "scene.sleep", "name": "睡眠", "state": "off", "device_class": None},
        ]))
        svc.list_scenes = AsyncMock(return_value=[{"id": "abc", "name": "睡眠", "actions": []}])
        resolved = await svc.resolve_scene(name="睡眠")
        assert resolved == {"kind": "ha", "id": "scene.sleep", "name": "睡眠"}

    async def test_local_fallback_when_ha_misses(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub([]))
        svc.list_scenes = AsyncMock(return_value=[{"id": "abc", "name": "观影", "actions": []}])
        resolved = await svc.resolve_scene(name="观影")
        assert resolved == {"kind": "local", "id": "abc", "name": "观影"}

    async def test_by_scene_id_forms(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub())
        assert (await svc.resolve_scene(scene_id="scene.movie"))["kind"] == "ha"

        async def fake_get(sid: str):
            return {"id": "abc", "name": "观影", "actions": []} if sid == "abc" else None
        svc.get_scene = fake_get
        assert (await svc.resolve_scene(scene_id="abc"))["kind"] == "local"
        assert await svc.resolve_scene(scene_id="nope") is None

    async def test_no_match_returns_none(self):
        svc = SceneService(ha_client=MagicMock(), ha_service=_ha_stub([]))
        svc.list_scenes = AsyncMock(return_value=[])
        assert await svc.resolve_scene(name="不存在") is None
        assert await svc.resolve_scene() is None


class TestSceneRouteOrder:
    def test_ha_apply_registered_before_param_route(self):
        """/scenes/ha/apply 必须先注册，否则 POST /scenes/ha/apply 会被
        /scenes/{scene_id}/apply 吞成 scene_id="ha"。"""
        from app.routes import scene_routes
        paths = [(r.path, getattr(r, "methods", set())) for r in scene_routes.router.routes]
        ha_idx = next(i for i, (p, m) in enumerate(paths) if p == "/scenes/ha/apply")
        param_idx = next(i for i, (p, m) in enumerate(paths) if p == "/scenes/{scene_id}/apply")
        assert ha_idx < param_idx


# ===================== 天气降级链 =====================

def _cfg(monkeypatch, *, weather: dict | None = None, home: dict | None = None,
         ha_entity: str = ""):
    """接管 weather_service.get_config：weather 段 / home 段 / weather.ha_entity。"""
    import app.services.weather_service as ws

    def fake_get_config(key, default=None):
        if key == "weather":
            return weather if weather is not None else {}
        if key == "weather.ha_entity":
            return ha_entity
        if key == "home":
            return home if home is not None else {}
        return default

    monkeypatch.setattr(ws, "get_config", fake_get_config)


def _db(monkeypatch, cached: str | None = None):
    import app.services.weather_service as ws
    db = MagicMock()
    db.kv_get = AsyncMock(return_value=cached)
    db.kv_set = AsyncMock()
    MockDB = MagicMock()
    MockDB.get = staticmethod(lambda: db)
    monkeypatch.setattr(ws, "Database", MockDB)
    return db


_QWEATHER_FULL = {"host": "h", "kid": "k", "sub": "s", "private_key": "p"}


class TestWeatherFallbackChain:
    async def test_ha_fallback_when_qweather_unconfigured_home(self, monkeypatch):
        """和风未配置 + 家庭位置（home 配了城市）→ HA 兜底成功。"""
        import app.services.weather_service as ws
        _cfg(monkeypatch, home={"province": "上海", "city": "上海", "district": ""},
             ha_entity="weather.home")
        _db(monkeypatch)
        client = MagicMock()
        client.get_state = AsyncMock(return_value={
            "entity_id": "weather.home", "state": "partlycloudy",
            "attributes": {"friendly_name": "Home", "temperature": 24,
                           "apparent_temperature": 23, "humidity": 65,
                           "wind_direction": "东南", "wind_speed": 12.0},
        })
        ha = _ha_stub(entities=[{"entity_id": "weather.home", "name": "Home",
                                 "state": "partlycloudy", "device_class": None}],
                      client=client)
        monkeypatch.setattr(ws, "_ha_service_ref", [ha])
        result = await ws.get_weather()
        assert result["source"] == "ha"
        assert result["weather"] == "多云"
        assert result["temperature"] == 24
        assert result["wind_scale"] == "3"  # 12 km/h → 蒲福 3 级（6-11 为 2 级）
        assert result["indices"] == []

    async def test_no_home_config_goes_straight_to_ha(self, monkeypatch):
        import app.services.weather_service as ws
        _cfg(monkeypatch, home={})
        _db(monkeypatch)
        client = MagicMock()
        client.get_state = AsyncMock(return_value={
            "entity_id": "weather.home", "state": "sunny", "attributes": {}})
        ha = _ha_stub(client=client)
        ha.get_entities_by_domains = AsyncMock(return_value=[
            {"entity_id": "weather.home", "name": "Home", "state": "sunny",
             "device_class": None}])
        monkeypatch.setattr(ws, "_ha_service_ref", [ha])
        result = await ws.get_weather()
        assert result["source"] == "ha"

    async def test_explicit_city_does_not_fall_back_to_ha(self, monkeypatch):
        """显式指定城市：和风未配置 → 直接报错（HA 查不了别的城市）。"""
        import app.services.weather_service as ws
        _cfg(monkeypatch)
        _db(monkeypatch)
        monkeypatch.setattr(ws, "_ha_service_ref", [MagicMock()])
        result = await ws.get_weather("北京")
        assert "error" in result
        assert "和风" in result["error"]

    async def test_qweather_success_wins(self, monkeypatch):
        import app.services.weather_service as ws
        _cfg(monkeypatch, weather=_QWEATHER_FULL,
             home={"city": "上海"})
        db = _db(monkeypatch)
        monkeypatch.setattr(ws, "_ha_service_ref", [MagicMock()])

        async def fake_fetch(location):
            return {"location": "上海", "weather": "晴", "temperature": "30",
                    "indices": [{"name": "运动"}], "source": "qweather"}
        monkeypatch.setattr(ws, "_fetch_qweather", fake_fetch)
        result = await ws.get_weather()
        assert result["source"] == "qweather"
        assert result["indices"] == [{"name": "运动"}]
        db.kv_set.assert_awaited_once()  # 成功结果写缓存

    async def test_qweather_failure_falls_back_to_ha(self, monkeypatch):
        import app.services.weather_service as ws
        _cfg(monkeypatch, weather=_QWEATHER_FULL,
             home={"city": "上海"}, ha_entity="weather.home")
        _db(monkeypatch)
        client = MagicMock()
        client.get_state = AsyncMock(return_value={
            "entity_id": "weather.home", "state": "rainy",
            "attributes": {"friendly_name": "Home"}})
        monkeypatch.setattr(ws, "_ha_service_ref", [_ha_stub(client=client)])

        async def failing_fetch(location):
            raise RuntimeError("quota")
        monkeypatch.setattr(ws, "_fetch_qweather", failing_fetch)
        result = await ws.get_weather()
        assert result["source"] == "ha" and result["weather"] == "雨"

    async def test_all_sources_down_reports_unavailable(self, monkeypatch):
        import app.services.weather_service as ws
        _cfg(monkeypatch, home={"city": "上海"})
        _db(monkeypatch)
        monkeypatch.setattr(ws, "_ha_service_ref", [None])
        result = await ws.get_weather()
        assert "error" in result and "不可用" in result["error"]

    async def test_ha_no_entity_returns_none(self, monkeypatch):
        import app.services.weather_service as ws
        _cfg(monkeypatch)
        monkeypatch.setattr(ws, "_ha_service_ref", [_ha_stub(entities=[])])
        assert await ws._get_ha_weather() is None

    async def test_cache_shared_between_sources(self, monkeypatch):
        """HA 兜底结果也写同一缓存 key，下轮命中不再出门。"""
        import app.services.weather_service as ws
        _cfg(monkeypatch, home={"city": "上海"}, ha_entity="weather.home")
        db = _db(monkeypatch)
        client = MagicMock()
        client.get_state = AsyncMock(return_value={
            "entity_id": "weather.home", "state": "sunny",
            "attributes": {"friendly_name": "Home"}})
        monkeypatch.setattr(ws, "_ha_service_ref", [_ha_stub(client=client)])
        first = await ws.get_weather()
        assert first["source"] == "ha"
        db.kv_set.assert_awaited_once()
        # 模拟缓存命中：kv_get 返回刚写入的结构
        db.kv_get = AsyncMock(return_value=json.dumps(
            {"weather": first, "cached_at": time.time()}))
        client.get_state = AsyncMock(side_effect=RuntimeError("should not be called"))
        second = await ws.get_weather()
        assert second["source"] == "ha"


class TestWeatherHelpers:
    def test_beaufort(self):
        from app.services.weather_service import _beaufort_from_kmh
        assert _beaufort_from_kmh(None) == ""
        assert _beaufort_from_kmh("abc") == ""
        assert _beaufort_from_kmh(1) == "1"
        assert _beaufort_from_kmh(12) == "3"
        assert _beaufort_from_kmh(100) == "10"
        assert _beaufort_from_kmh(200) == "12"

    def test_condition_zh(self):
        from app.services.weather_service import _HA_CONDITION_ZH, _get_ha_weather
        assert _HA_CONDITION_ZH["partlycloudy"] == "多云"
        assert _HA_CONDITION_ZH["pouring"] == "暴雨"
        assert _HA_CONDITION_ZH["clear-night"] == "晴夜"
