"""CameraStream 参数化重构单测(Task 2)。

验证 CameraStream 从「读全局 config」改为「读 config dict」:
- camera_id 来自构造参数
- motion_threshold / min_infer_interval 从 config dict 读
- display_enabled 开关 + start/stop_display 薄封装
- 自动化触发回调带 camera_id
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.camera_stream import CameraStream


def _config(overrides: dict | None = None) -> dict:
    """单路配置 dict(cameras 表行映射)。"""
    base = {
        "id": "cam_test01", "source_type": "rtsp",
        "rtsp_url": "rtsp://1.2.3.4/stream", "rtsp_username": "admin",
        "rtsp_password": "pwd", "usb_index": 0,
        "motion_hash_size": 16, "motion_threshold": 15, "motion_check_interval": 1.0,
        "vision_min_infer_interval": 8.0, "vision_max_idle_interval": 120.0,
        "vision_use_img_count": 3, "frame_interval_ms": 2000, "display_enabled": 1,
    }
    base.update(overrides or {})
    return base


class TestCameraStreamConstruction:
    def test_reads_camera_id_from_config(self):
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        assert s.camera_id == "cam_test01"

    def test_reads_params_from_config_dict(self):
        """不读全局 get_config,核心参数从 config dict 读。"""
        cfg = _config({"motion_threshold": 25, "vision_min_infer_interval": 12.0})
        s = CameraStream("cam_test01", cfg, vision_service=MagicMock())
        assert s._motion.threshold == 25
        assert s._min_infer_interval == 12.0

    def test_rtsp_credentials_from_config(self):
        """RTSP 凭证从 config 读(不再走 env 变量名)。"""
        cfg = _config({"rtsp_url": "rtsp://h/path", "rtsp_username": "u", "rtsp_password": "secret"})
        s = CameraStream("cam_test01", cfg, vision_service=MagicMock())
        assert s._rtsp_url == "rtsp://h/path"
        assert s._rtsp_username == "u"
        assert s._rtsp_password == "secret"

    def test_resolve_rtsp_url_embeds_credentials(self):
        cfg = _config({"rtsp_url": "rtsp://1.2.3.4/stream", "rtsp_username": "u", "rtsp_password": "p"})
        s = CameraStream("cam_test01", cfg, vision_service=MagicMock())
        assert s._resolve_rtsp_url() == "rtsp://u:p@1.2.3.4/stream"

    def test_resolve_rtsp_url_without_credentials(self):
        """无凭证裸连(部分摄像头 RTSP 不要求鉴权)。"""
        cfg = _config({"rtsp_url": "rtsp://1.2.3.4/stream", "rtsp_username": "", "rtsp_password": ""})
        s = CameraStream("cam_test01", cfg, vision_service=MagicMock())
        assert s._resolve_rtsp_url() == "rtsp://1.2.3.4/stream"

    def test_usb_source_when_no_rtsp(self):
        """无 rtsp_url 时走 usb_index。"""
        cfg = _config({"source_type": "usb", "rtsp_url": "", "usb_index": 2})
        s = CameraStream("cam_test01", cfg, vision_service=MagicMock())
        assert s._rtsp_url == ""
        assert s._camera_index == 2


class TestDisplaySwitch:
    def test_display_enabled_from_config(self):
        s = CameraStream("cam_test01", _config({"display_enabled": 0}), vision_service=MagicMock())
        assert s._camera_vl_display_enabled is False

    def test_set_display_enabled_toggles_flag(self):
        s = CameraStream("cam_test01", _config({"display_enabled": 0}), vision_service=MagicMock())
        assert s._camera_vl_display_enabled is False
        s.set_display_enabled(True)
        assert s._camera_vl_display_enabled is True
        s.set_display_enabled(False)
        assert s._camera_vl_display_enabled is False

    def test_start_stop_display_thin_wrappers(self):
        """D4:start_display/stop_display 是 set_display_enabled 的薄封装。"""
        s = CameraStream("cam_test01", _config({"display_enabled": 0}), vision_service=MagicMock())
        assert s._camera_vl_display_enabled is False
        s.start_display()
        assert s._camera_vl_display_enabled is True
        s.stop_display()
        assert s._camera_vl_display_enabled is False


class TestAutomationTriggerCallback:
    def test_callback_receives_camera_id(self):
        """回调签名变 callback(camera_id),触发时带本路 id。"""
        received: list[str] = []
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        s.set_on_automation_trigger(lambda cid: received.append(cid))
        # 模拟运动触发(直接调回调,等同 worker 内 _on_automation_trigger(self.camera_id))
        s._on_automation_trigger(s.camera_id)
        assert received == ["cam_test01"]

    def test_default_callback_is_none_safe(self):
        """未注册回调时不崩。"""
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        # _on_automation_trigger 初始为 None;worker 内会先判 None 再调
        assert s._on_automation_trigger is None


class TestCameraStateHasId:
    def test_state_carries_camera_id(self):
        """get_state 返回的 dict 带 camera_id。"""
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        st = s.get_state()
        assert st["camera_id"] == "cam_test01"
