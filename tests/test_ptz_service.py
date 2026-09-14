"""Tests for app/services/ptz_service.py — ONVIF PTZ 控制。

extract_host_from_url 已在 test_ptz_config.py 覆盖，此处聚焦：
- PtzService.move/stop/step 的方向校验与连接守卫
- _ensure_connected 的懒加载 / 断线重连 / enabled 守卫
- _speed 的钳制
- step 的 token 交权机制

onvif-zeep-async 4.x 的 ONVIFCamera 是 async API，ptz_service 全 async，
service 方法（GetProfiles/ContinuousMove/Stop）也是 async，测试用 AsyncMock。

构造统一走 _svc()：全局 config("ptz.*") 回退分支已删，PtzService 只认
cameras 行的 ptz_* 字段（与 PtzRegistry 的注入方式一致）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ptz_service import PtzService, _DIRECTION_VECTORS, extract_host_from_url

_BASE_CFG = {
    "ptz_enabled": 1,
    "ptz_ip": "10.0.0.1",
    "ptz_port": 80,
    "ptz_username": "u",
    "ptz_password": "p",
    "ptz_speed": 0.5,
}


def _svc(overrides: dict | None = None) -> PtzService:
    cfg = {**_BASE_CFG, **(overrides or {})}
    return PtzService("cam1", cfg)


class TestDirectionVectors:
    """方向向量定义正确性。"""

    def test_four_directions_exist(self):
        for d in ("up", "down", "left", "right"):
            assert d in _DIRECTION_VECTORS

    def test_up_is_positive_tilt(self):
        assert _DIRECTION_VECTORS["up"] == (0.0, 1.0)

    def test_down_is_negative_tilt(self):
        assert _DIRECTION_VECTORS["down"] == (0.0, -1.0)

    def test_left_is_negative_pan(self):
        assert _DIRECTION_VECTORS["left"] == (-1.0, 0.0)

    def test_right_is_positive_pan(self):
        assert _DIRECTION_VECTORS["right"] == (1.0, 0.0)


class TestMoveUnknownDirection:
    """未知方向直接返回失败，不建连。"""

    @pytest.mark.asyncio
    async def test_move_unknown_direction(self):
        svc = _svc()
        result = await svc.move("sideways")
        assert result["success"] is False
        assert "unknown direction" in result["error"]

    @pytest.mark.asyncio
    async def test_step_unknown_direction(self):
        svc = _svc()
        result = await svc.step("diagonal", 100)
        assert result["success"] is False
        assert "unknown direction" in result["error"]


class TestEnsureConnected:
    """_ensure_connected 守卫逻辑。"""

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self):
        svc = _svc({"ptz_enabled": 0})
        assert await svc._ensure_connected() is False

    @pytest.mark.asyncio
    async def test_no_ip_returns_false(self):
        svc = _svc({"ptz_ip": ""})
        assert await svc._ensure_connected() is False

    @pytest.mark.asyncio
    async def test_connect_failure_marks_broken(self):
        svc = _svc()

        onvif_mod = MagicMock()
        onvif_mod.ONVIFCamera = MagicMock(side_effect=Exception("connect fail"))
        onvif_mod.__file__ = "/fake/onvif/__init__.py"
        with patch.dict("sys.modules", {"onvif": onvif_mod}):
            assert await svc._ensure_connected() is False
            assert svc._broken is True

    @pytest.mark.asyncio
    async def test_successful_connect(self):
        svc = _svc()

        # mock ONVIFCamera + media/ptz service（4.x service 方法是 async）
        profile = MagicMock()
        profile.token = "profile-0"
        media = MagicMock()
        media.GetProfiles = AsyncMock(return_value=[profile])
        cam = MagicMock()
        cam.update_xaddrs = AsyncMock(return_value=None)
        cam.create_media_service = AsyncMock(return_value=media)
        cam.create_ptz_service = AsyncMock(return_value=MagicMock())

        onvif_mod = MagicMock()
        onvif_mod.ONVIFCamera = MagicMock(return_value=cam)
        onvif_mod.__file__ = "/fake/onvif/__init__.py"
        with patch.dict("sys.modules", {"onvif": onvif_mod}):
            assert await svc._ensure_connected() is True
            assert svc._broken is False
            assert svc._profile_token == "profile-0"

    @pytest.mark.asyncio
    async def test_no_profiles_marks_broken(self):
        svc = _svc()

        media = MagicMock()
        media.GetProfiles = AsyncMock(return_value=[])  # 无 profile
        cam = MagicMock()
        cam.update_xaddrs = AsyncMock(return_value=None)
        cam.create_media_service = AsyncMock(return_value=media)

        onvif_mod = MagicMock()
        onvif_mod.ONVIFCamera = MagicMock(return_value=cam)
        onvif_mod.__file__ = "/fake/onvif/__init__.py"
        with patch.dict("sys.modules", {"onvif": onvif_mod}):
            assert await svc._ensure_connected() is False
            assert svc._broken is True


class TestSpeedClamping:
    """_speed 钳制到 [0.1, 1.0]。"""

    @pytest.mark.parametrize("cfg_val,expected", [
        (0.5, 0.5),
        (0.0, 0.1),   # 下限
        (-1.0, 0.1),  # 低于下限
        (1.5, 1.0),   # 上限
        (2.0, 1.0),   # 超上限
    ])
    def test_speed_clamped(self, cfg_val, expected):
        svc = _svc({"ptz_speed": cfg_val})
        assert svc._speed() == expected


class TestStop:
    """stop() 在未连接时返回失败。"""

    @pytest.mark.asyncio
    async def test_stop_not_connected(self):
        svc = _svc()
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=False)):
            result = await svc.stop()
            assert result["success"] is False
            assert "not connected" in result["error"]

    @pytest.mark.asyncio
    async def test_stop_connected_returns_success(self):
        svc = _svc()
        svc._ptz = MagicMock()
        svc._ptz.Stop = AsyncMock(return_value=None)
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)):
            result = await svc.stop()
            assert result["success"] is True
            svc._ptz.Stop.assert_called_once()


class TestMoveConnected:
    """move() 已连接时发 ContinuousMove。"""

    @pytest.mark.asyncio
    async def test_move_success(self):
        svc = _svc()
        svc._ptz = MagicMock()
        svc._ptz.ContinuousMove = AsyncMock(return_value=None)
        svc._ptz.Stop = AsyncMock(return_value=None)
        svc._ptz.create_type = MagicMock(return_value=MagicMock())
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)), \
             patch.object(svc, "_speed", return_value=0.5):
            result = await svc.move("up")
            assert result["success"] is True
            assert result["direction"] == "up"
            # ContinuousMove 被调
            svc._ptz.ContinuousMove.assert_called_once()
            # Stop 也被调（清除残留）
            svc._ptz.Stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_move_failure_marks_broken(self):
        svc = _svc()
        svc._ptz = MagicMock()
        svc._ptz.ContinuousMove = AsyncMock(side_effect=Exception("move fail"))
        svc._ptz.Stop = AsyncMock(return_value=None)
        svc._ptz.create_type = MagicMock(return_value=MagicMock())
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)), \
             patch.object(svc, "_speed", return_value=0.5):
            result = await svc.move("right")
            assert result["success"] is False
            assert "move fail" in result["error"]
            assert svc._broken is True


class TestStep:
    """step() 步进：move → 等待 → auto-stop。"""

    @pytest.mark.asyncio
    async def test_step_success_short_duration(self):
        svc = _svc()
        svc._ptz = MagicMock()
        svc._ptz.ContinuousMove = AsyncMock(return_value=None)
        svc._ptz.Stop = AsyncMock(return_value=None)
        svc._ptz.create_type = MagicMock(return_value=MagicMock())
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)), \
             patch.object(svc, "_speed", return_value=0.5):
            result = await svc.step("left", 30)  # 30ms
            assert result["success"] is True
            assert "interrupted" not in result
            # 到点后 Stop 被调
            svc._ptz.Stop.assert_called()

    @pytest.mark.asyncio
    async def test_step_interrupted_by_new_step(self):
        """新 step 到来 → 旧 step 提前交权，不发 Stop。"""
        svc = _svc()
        svc._ptz = MagicMock()
        svc._ptz.ContinuousMove = AsyncMock(return_value=None)
        svc._ptz.Stop = AsyncMock(return_value=None)
        svc._ptz.create_type = MagicMock(return_value=MagicMock())
        svc._profile_token = "tok"

        with patch.object(svc, "_ensure_connected", new=AsyncMock(return_value=True)), \
             patch.object(svc, "_speed", return_value=0.5):
            # 用 task 模拟新 step 打断
            async def interrupt():
                await asyncio.sleep(0.05)
                svc._step_token += 1  # 模拟新 step 接管

            t = asyncio.create_task(interrupt())
            result = await svc.step("up", 500)  # 500ms 但会被打断
            await t
            assert result["success"] is True
            assert result.get("interrupted") is True


class TestNotifyIpChanged:
    """notify_ip_changed: 作废缓存连接,下次 _ensure_connected 用行内新 IP 重连。"""

    @pytest.mark.asyncio
    async def test_marks_broken_and_clears_connection(self):
        svc = _svc()
        # 模拟已有连接
        svc._cam = MagicMock()
        svc._ptz = MagicMock()
        svc._profile_token = "tok"
        svc._broken = False
        svc.notify_ip_changed("192.168.1.99")
        assert svc._broken is True
        assert svc._cam is None
        assert svc._ptz is None
        assert svc._profile_token is None
