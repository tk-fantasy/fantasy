"""Tests for app/services/ptz_service.py — ONVIF PTZ 控制。

extract_host_from_url 已在 test_ptz_config.py 覆盖，此处聚焦：
- PtzService.move/stop/step 的方向校验与连接守卫
- _ensure_connected 的懒加载 / 断线重连 / enabled 守卫
- _speed 的钳制
- step 的 token 交权机制
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, ANY

import pytest

from app.services.ptz_service import PtzService, _DIRECTION_VECTORS, extract_host_from_url


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

    def test_move_unknown_direction(self):
        svc = PtzService()
        result = svc.move("sideways")
        assert result["success"] is False
        assert "unknown direction" in result["error"]

    def test_step_unknown_direction(self):
        svc = PtzService()
        result = svc.step("diagonal", 100)
        assert result["success"] is False
        assert "unknown direction" in result["error"]


class TestEnsureConnected:
    """_ensure_connected 守卫逻辑。"""

    def test_disabled_returns_false(self):
        svc = PtzService()
        with patch("app.services.ptz_service.get_config", return_value=False):
            assert svc._ensure_connected() is False

    def test_no_ip_returns_false(self):
        svc = PtzService()
        cfg = {"ptz.enabled": True, "ptz.ip": "", "ptz.port": 80,
               "ptz.username": "u", "ptz.password_env": "PWD"}
        with patch("app.services.ptz_service.get_config", side_effect=lambda p, d=None: cfg.get(p, d)):
            assert svc._ensure_connected() is False

    def test_connect_failure_marks_broken(self):
        svc = PtzService()
        cfg = {"ptz.enabled": True, "ptz.ip": "10.0.0.1", "ptz.port": 80,
               "ptz.username": "u", "ptz.password_env": "PWD"}

        with patch("app.services.ptz_service.get_config", side_effect=lambda p, d=None: cfg.get(p, d)), \
             patch.dict("sys.modules", {"onvif": MagicMock(ONVIFCamera=MagicMock(side_effect=Exception("connect fail")))}):
            assert svc._ensure_connected() is False
            assert svc._broken is True

    def test_successful_connect(self):
        svc = PtzService()
        cfg = {"ptz.enabled": True, "ptz.ip": "10.0.0.1", "ptz.port": 80,
               "ptz.username": "u", "ptz.password_env": "PWD"}

        # mock ONVIFCamera + media/ptz service
        profile = MagicMock()
        profile.token = "profile-0"
        media = MagicMock()
        media.GetProfiles.return_value = [profile]
        cam = MagicMock()
        cam.create_media_service.return_value = media
        cam.create_ptz_service.return_value = MagicMock()

        with patch("app.services.ptz_service.get_config", side_effect=lambda p, d=None: cfg.get(p, d)), \
             patch.dict("sys.modules", {"onvif": MagicMock(ONVIFCamera=MagicMock(return_value=cam))}):
            assert svc._ensure_connected() is True
            assert svc._broken is False
            assert svc._profile_token == "profile-0"

    def test_no_profiles_marks_broken(self):
        svc = PtzService()
        cfg = {"ptz.enabled": True, "ptz.ip": "10.0.0.1", "ptz.port": 80,
               "ptz.username": "u", "ptz.password_env": "PWD"}

        media = MagicMock()
        media.GetProfiles.return_value = []  # 无 profile
        cam = MagicMock()
        cam.create_media_service.return_value = media

        with patch("app.services.ptz_service.get_config", side_effect=lambda p, d=None: cfg.get(p, d)), \
             patch.dict("sys.modules", {"onvif": MagicMock(ONVIFCamera=MagicMock(return_value=cam))}):
            assert svc._ensure_connected() is False
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
        svc = PtzService()
        with patch("app.services.ptz_service.get_config", return_value=cfg_val):
            assert svc._speed() == expected


class TestStop:
    """stop() 在未连接时返回失败。"""

    def test_stop_not_connected(self):
        svc = PtzService()
        with patch.object(svc, "_ensure_connected", return_value=False):
            result = svc.stop()
            assert result["success"] is False
            assert "not connected" in result["error"]

    def test_stop_connected_returns_success(self):
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", return_value=True):
            result = svc.stop()
            assert result["success"] is True
            svc._ptz.Stop.assert_called_once()


class TestMoveConnected:
    """move() 已连接时发 ContinuousMove。"""

    def test_move_success(self):
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", return_value=True), \
             patch.object(svc, "_speed", return_value=0.5):
            result = svc.move("up")
            assert result["success"] is True
            assert result["direction"] == "up"
            # ContinuousMove 被调
            svc._ptz.ContinuousMove.assert_called_once()
            # Stop 也被调（清除残留）
            svc._ptz.Stop.assert_called_once()

    def test_move_failure_marks_broken(self):
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._ptz.ContinuousMove.side_effect = Exception("move fail")
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", return_value=True), \
             patch.object(svc, "_speed", return_value=0.5):
            result = svc.move("right")
            assert result["success"] is False
            assert "move fail" in result["error"]
            assert svc._broken is True


class TestStep:
    """step() 步进：move → 等待 → auto-stop。"""

    def test_step_success_short_duration(self):
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._profile_token = "tok"
        with patch.object(svc, "_ensure_connected", return_value=True), \
             patch.object(svc, "_speed", return_value=0.5):
            result = svc.step("left", 30)  # 30ms
            assert result["success"] is True
            assert "interrupted" not in result
            # 到点后 Stop 被调
            svc._ptz.Stop.assert_called()

    def test_step_interrupted_by_new_step(self):
        """新 step 到来 → 旧 step 提前交权，不发 Stop。"""
        svc = PtzService()
        svc._ptz = MagicMock()
        svc._profile_token = "tok"

        with patch.object(svc, "_ensure_connected", return_value=True), \
             patch.object(svc, "_speed", return_value=0.5):
            # 用线程模拟新 step 打断
            import threading

            def interrupt():
                time.sleep(0.05)
                svc._step_token += 1  # 模拟新 step 接管

            t = threading.Thread(target=interrupt)
            t.start()
            result = svc.step("up", 500)  # 500ms 但会被打断
            t.join()
            assert result["success"] is True
            assert result.get("interrupted") is True
