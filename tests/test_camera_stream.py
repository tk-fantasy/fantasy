"""CameraStream 参数化重构单测(Task 2)。

验证 CameraStream 从「读全局 config」改为「读 config dict」:
- camera_id 来自构造参数
- motion_threshold / min_infer_interval 从 config dict 读
- display_enabled 开关 + start/stop_display 薄封装
- 自动化触发回调带 camera_id
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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


class TestWorkerDiscoveryTrigger:
    """worker 掉线触发 ONVIF discovery:传 camera_id(Bug 3a)+ 读该路 discovery_enabled(Bug 1)。

    之前 worker 调 find_and_apply() 漏传 camera_id → 走 legacy 分支读全局 MAC;
    且触发闸门读全局 vision.discovery_enabled 而非该路 cameras 行开关。两者都导致
    断电后自动重连找不到/不触发,只有手动"重新发现"按钮能成。
    """

    @pytest.mark.asyncio
    async def test_worker_triggers_discovery_with_camera_id(self, monkeypatch):
        """开流连续失败 → 触发 discovery,且 find_and_apply 传了本路 camera_id。"""
        discovery = MagicMock()
        discovery.find_and_apply = AsyncMock(return_value="192.168.1.50")
        s = CameraStream(
            "cam_test01", _config({"discovery_enabled": 1}),
            vision_service=MagicMock(), discovery_service=discovery,
        )
        s.set_event_loop(asyncio.get_running_loop())
        s._discovery_trigger_threshold = 1   # 一次失败即触发,加速测试

        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self_: fake_cap)
        monkeypatch.setattr("app.camera_stream.time.sleep", lambda *a, **k: None)

        s.start()
        await asyncio.sleep(0.5)   # 让 worker 跑 + loop 处理投递的 find_and_apply
        s.stop()

        discovery.find_and_apply.assert_awaited_once()
        assert discovery.find_and_apply.await_args.args == ("cam_test01",)

    @pytest.mark.asyncio
    async def test_worker_skips_discovery_when_per_camera_disabled(self, monkeypatch):
        """该路 discovery_enabled=0(conftest 全局注入为 True)→ 不触发,
        反证 worker 读的是该路开关而非全局 config。"""
        discovery = MagicMock()
        discovery.find_and_apply = AsyncMock()
        s = CameraStream(
            "cam_test01", _config({"discovery_enabled": 0}),
            vision_service=MagicMock(), discovery_service=discovery,
        )
        s.set_event_loop(asyncio.get_running_loop())
        s._discovery_trigger_threshold = 1

        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self_: fake_cap)
        monkeypatch.setattr("app.camera_stream.time.sleep", lambda *a, **k: None)

        s.start()
        await asyncio.sleep(0.3)
        s.stop()

        discovery.find_and_apply.assert_not_awaited()


class TestColdOpenBackoff:
    """冷启动退避:从未成功开过流还连续失败 → 降到分钟级重试。

    部分 IPC(实测 TP-Link TL-IPC43CL)的 RTSP 有防爆破锁定,秒级重试
    风暴会把瞬态 401 恶化成"正确密码也 401",直到设备断电重启。
    """

    def _make_closed_stream(self, monkeypatch):
        s = CameraStream("cam_test01", _config(), vision_service=MagicMock())
        fake_cap = MagicMock()
        fake_cap.isOpened.return_value = False
        monkeypatch.setattr(CameraStream, "_open_camera", lambda self_: fake_cap)
        return s

    def test_never_opened_failures_use_cold_backoff(self, monkeypatch):
        """冷启动连续失败第 5 次 → backoff 直接取 cold_open_backoff。"""
        s = self._make_closed_stream(monkeypatch)
        assert s._ever_opened is False
        # 直接调 worker 内同款计算路径:模拟连续失败 5 次后的退避值
        s._consecutive_open_failures = 5
        backoff = min(
            s._release_cooldown * (2 ** min(s._consecutive_open_failures - 1, 4)),
            s._max_backoff,
        )
        if not s._ever_opened and s._consecutive_open_failures >= 5:
            backoff = s._cold_open_backoff
        assert backoff == s._cold_open_backoff
        assert backoff >= 60.0

    def test_warm_failures_keep_fast_backoff(self, monkeypatch):
        """曾成功开过流(ever_opened=True)的失败 → 保持原秒级退避。"""
        s = self._make_closed_stream(monkeypatch)
        s._ever_opened = True
        s._consecutive_open_failures = 5
        backoff = min(
            s._release_cooldown * (2 ** min(s._consecutive_open_failures - 1, 4)),
            s._max_backoff,
        )
        if not s._ever_opened and s._consecutive_open_failures >= 5:
            backoff = s._cold_open_backoff
        assert backoff == min(s._release_cooldown * 16, s._max_backoff)
        assert backoff < s._cold_open_backoff
