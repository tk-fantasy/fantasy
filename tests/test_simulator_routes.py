"""Tests for /api/simulator/* routes — 虚拟设备（模拟器 + MQTT）开关。

通过 patch Docker socket 相关函数，覆盖三个核心场景：
1. docker.sock 不可用 → status 返回 available=False
2. 容器运行/停止状态上报
3. stop / start 动作正确下发（先停模拟器、先启 mosquitto 的顺序由路由实现保证）
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.routes import simulator_routes


class TestSimulatorStatus:
    @pytest.mark.asyncio
    async def test_status_socket_unavailable(self):
        """docker.sock 不存在 → available=False，前端据此隐藏开关。"""
        with patch.object(simulator_routes, "docker_socket_available", return_value=False):
            result = await simulator_routes.simulator_status()

        data = result.data
        assert data["available"] is False
        assert data["running"] is False

    @pytest.mark.asyncio
    async def test_status_both_running(self):
        """两个容器都运行 → available=True, running=True。"""
        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_state") as mock_state:
            mock_state.side_effect = lambda name: {
                "available": True,
                "exists": True,
                "running": True,
            }
            result = await simulator_routes.simulator_status()

        assert result.data["available"] is True
        assert result.data["running"] is True

    @pytest.mark.asyncio
    async def test_status_partial_running(self):
        """只有一个容器运行 → running=False（需要两个都起来才算在线）。"""
        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_state") as mock_state:
            mock_state.side_effect = lambda name: {
                "available": True,
                "exists": True,
                "running": name == simulator_routes.SIMULATOR_CONTAINER,
            }
            result = await simulator_routes.simulator_status()

        assert result.data["running"] is False

    @pytest.mark.asyncio
    async def test_status_container_missing(self):
        """容器不存在（compose 未启动）→ exists=False。"""
        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_state") as mock_state:
            mock_state.side_effect = lambda name: {
                "available": True,
                "exists": False,
                "running": False,
            }
            result = await simulator_routes.simulator_status()

        assert result.data["available"] is True
        assert result.data["running"] is False
        assert result.data["simulator"]["exists"] is False


class TestSimulatorStopStart:
    @pytest.mark.asyncio
    async def test_stop_calls_both_containers(self):
        """stop 应对 simulator 和 mosquitto 各下发一次 stop。"""
        calls = []

        async def fake_action(name, action):
            calls.append((name, action))
            return {"available": True, "ok": True}

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            result = await simulator_routes.simulator_stop()

        assert result.data["ok"] is True
        assert (simulator_routes.SIMULATOR_CONTAINER, "stop") in calls
        assert (simulator_routes.MQTT_CONTAINER, "stop") in calls

    @pytest.mark.asyncio
    async def test_start_calls_both_containers(self):
        """start 应对 mosquitto 和 simulator 各下发一次 start。"""
        calls = []

        async def fake_action(name, action):
            calls.append((name, action))
            return {"available": True, "ok": True}

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            result = await simulator_routes.simulator_start()

        assert result.data["ok"] is True
        assert (simulator_routes.MQTT_CONTAINER, "start") in calls
        assert (simulator_routes.SIMULATOR_CONTAINER, "start") in calls

    @pytest.mark.asyncio
    async def test_stop_partial_failure(self):
        """部分失败 → ok=False，code=partial。"""
        async def fake_action(name, action):
            return {"available": True, "ok": False, "error": "HTTP 500"}

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            result = await simulator_routes.simulator_stop()

        assert result.data["ok"] is False
        assert result.code == "partial"

    @pytest.mark.asyncio
    async def test_stop_socket_unavailable(self):
        """socket 不可用 → ok=False 且不触发 docker 调用。"""
        with patch.object(simulator_routes, "docker_socket_available", return_value=False), \
             patch.object(simulator_routes, "_container_action") as mock_action:
            result = await simulator_routes.simulator_stop()

        assert result.data["ok"] is False
        mock_action.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_refreshes_device_views(self):
        """stop 成功后应失效 HAService 状态缓存并触发 AI 目录刷新。"""
        async def fake_action(name, action):
            return {"available": True, "ok": True}

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            await simulator_routes.simulator_stop(container=container)

        container.ha_service.invalidate_states_cache.assert_called_once()
        container.catalog_refresh_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_refreshes_device_views(self):
        """start 成功后同样刷新设备视图。"""
        async def fake_action(name, action):
            return {"available": True, "ok": True}

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            await simulator_routes.simulator_start(container=container)

        container.ha_service.invalidate_states_cache.assert_called_once()
        container.catalog_refresh_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_partial_failure_skips_refresh(self):
        """部分失败（ok=False）时不刷新（避免用半同步状态重建目录）。"""
        async def fake_action(name, action):
            return {"available": True, "ok": False, "error": "HTTP 500"}

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            await simulator_routes.simulator_stop(container=container)

        container.ha_service.invalidate_states_cache.assert_not_called()
        container.catalog_refresh_fn.assert_not_awaited()
