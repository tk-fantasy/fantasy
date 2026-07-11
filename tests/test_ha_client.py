"""Tests for app/clients/ha_client.py — HomeAssistantClient。

用 httpx.MockTransport 模拟 HA REST API，验证：
- 请求构造（URL、Authorization header）
- get_states / call_service / get_services
- 公开只读属性 base_url / token（#8 修复回归保护）
- close 行为
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.clients import ha_client as ha_mod
from app.clients.ha_client import HomeAssistantClient


def _make_client(handler, base_url="http://ha:8123", token="tok"):
    """构造一个直接用 MockTransport 的 client（跳过 new_client 工厂）。"""
    c = HomeAssistantClient(base_url=base_url, token=token)
    transport = httpx.MockTransport(handler)
    c._client = httpx.AsyncClient(
        transport=transport, base_url=c._base_url,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        trust_env=False,
    )
    c._client_lock = asyncio.Lock()
    return c


class TestClientConstruction:
    """构造与配置读取。"""

    def test_defaults(self):
        c = HomeAssistantClient(base_url="http://ha.local:8123/", token="abc")
        assert c.base_url == "http://ha.local:8123"
        assert c.token == "abc"

    def test_base_url_strips_trailing_slash(self):
        assert HomeAssistantClient(base_url="http://x/", token="t").base_url == "http://x"

    def test_public_properties_return_private_values(self):
        """#8 回归：公开 getter 返回私有字段值，不暴露 _base_url/_token。"""
        c = HomeAssistantClient(base_url="http://ha:8123", token="secret")
        assert c.base_url == c._base_url
        assert c.token == c._token

    def test_empty_token(self):
        c = HomeAssistantClient(base_url="http://ha:8123", token="")
        assert c.token == ""


class TestGetStates:
    @pytest.mark.asyncio
    async def test_get_states_returns_list(self):
        states = [{"entity_id": "light.lamp", "state": "on"}]

        async def handler(request):
            assert request.url.path == "/api/states"
            assert request.headers["Authorization"] == "Bearer tok"
            return httpx.Response(200, json=states)

        c = _make_client(handler)
        assert await c.get_states() == states

    @pytest.mark.asyncio
    async def test_get_states_raises_on_500(self):
        async def handler(request):
            return httpx.Response(500, text="err")

        c = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await c.get_states()


class TestCallService:
    @pytest.mark.asyncio
    async def test_with_entity_id_and_data(self):
        captured = {}

        async def handler(request):
            assert request.url.path == "/api/services/light/turn_on"
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json=[{"entity_id": "light.lamp"}])

        c = _make_client(handler)
        result = await c.call_service("light", "turn_on", entity_id="light.lamp",
                                      data={"brightness": 100})
        assert result == [{"entity_id": "light.lamp"}]
        assert captured["payload"]["entity_id"] == "light.lamp"
        assert captured["payload"]["brightness"] == 100

    @pytest.mark.asyncio
    async def test_without_entity_id(self):
        captured = {}

        async def handler(request):
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={})

        c = _make_client(handler)
        await c.call_service("homeassistant", "restart")
        assert captured["payload"] == {}

    @pytest.mark.asyncio
    async def test_merges_entity_id_and_data(self):
        captured = {}

        async def handler(request):
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={})

        c = _make_client(handler)
        await c.call_service("climate", "set_temperature", entity_id="climate.ac",
                             data={"temperature": 24, "hvac_mode": "cool"})
        assert captured["payload"]["entity_id"] == "climate.ac"
        assert captured["payload"]["temperature"] == 24
        assert captured["payload"]["hvac_mode"] == "cool"

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        async def handler(request):
            return httpx.Response(404, text="not found")

        c = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await c.call_service("light", "turn_on", entity_id="light.x")


class TestGetServices:
    @pytest.mark.asyncio
    async def test_returns_service_definitions(self):
        svc_data = [{"domain": "light", "services": {"turn_on": {"fields": {"entity_id": {}}}}}]

        async def handler(request):
            assert request.url.path == "/api/services"
            return httpx.Response(200, json=svc_data)

        c = _make_client(handler)
        assert await c.get_services() == svc_data


class TestClose:
    @pytest.mark.asyncio
    async def test_close_sets_client_none(self):
        c = _make_client(lambda r: httpx.Response(200, json=[]))
        await c.close()
        assert c._client is None

    @pytest.mark.asyncio
    async def test_close_when_no_client_is_noop(self):
        c = HomeAssistantClient(base_url="http://ha:8123", token="tok")
        c._client = None
        await c.close()  # 不抛
