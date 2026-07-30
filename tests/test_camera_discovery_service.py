"""Tests for app/services/camera_discovery_service.py — ONVIF 设备发现。

覆盖:
- read_device_hardware_id: 单点 ONVIF 读 HardwareId(MAC),降级 SerialNumber
- infer_subnet: 从旧 IP 推 /24 子网
- _mac_match: MAC 比较(归一化大小写/分隔符)
- find_camera: 两段式扫描 + MAC 匹配 + 超时 + config 回写
- 首次配对: 无 MAC 时用现有 IP 读 MAC
- capture_mac_on_startup: bootstrap 首次捕获

ONVIF 调用全部 mock(onvif.ONVIFCamera 用 AsyncMock),端口探测 mock socket。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.camera_discovery_service import (
    CameraDiscoveryService,
    infer_subnet,
    normalize_mac,
)


class TestNormalizeMac:
    """MAC 归一化:不同格式(大小写/分隔符/带冒号不带)视为相等。"""

    def test_lowercase(self):
        assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"

    def test_strip_dashes(self):
        assert normalize_mac("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"

    def test_strip_colons(self):
        assert normalize_mac("aa:bb:cc:dd:ee:ff") == "aabbccddeeff"

    def test_empty(self):
        assert normalize_mac("") == ""


class TestReadHardwareId:
    """read_device_hardware_id: mock ONVIFCamera,验证 MAC 取值优先级与降级。

    优先级:GetNetworkInterfaces.HwAddress(真 MAC) > HardwareId(像 MAC 才用)
    > SerialNumber(兜底)。TP-Link 的 HardwareId 是版本号 "2.0"(非 MAC),
    SerialNumber 只是 MAC 尾,只有 GetNetworkInterfaces 给完整 MAC。
    """

    @staticmethod
    def _make_cam(devicemgmt):
        cam = MagicMock()
        cam.update_xaddrs = AsyncMock()
        cam.create_devicemgmt_service = AsyncMock(return_value=devicemgmt)
        return cam

    @pytest.mark.asyncio
    async def test_prefers_network_interface_mac(self):
        """GetNetworkInterfaces 给完整 MAC 时,优先用它(而非 HardwareId)。"""
        svc = CameraDiscoveryService()
        nic = MagicMock()
        nic.Enabled = True
        nic.Info.HwAddress = "60-a3-e3-de-e0-54"
        devicemgmt = AsyncMock()
        devicemgmt.GetNetworkInterfaces = AsyncMock(return_value=[nic])
        # HardwareId 是假 MAC(版本号),不该被采用
        info = MagicMock()
        info.HardwareId = "2.0"
        info.SerialNumber = "e3dee054"
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        with patch("onvif.ONVIFCamera", return_value=self._make_cam(devicemgmt)):
            result = await svc.read_device_hardware_id("192.168.4.16", 80, "admin", "pass")
        assert result == "60-a3-e3-de-e0-54"

    @pytest.mark.asyncio
    async def test_skips_disabled_nic(self):
        """禁用的网卡 MAC 不采用,继续降级。"""
        svc = CameraDiscoveryService()
        nic = MagicMock()
        nic.Enabled = False
        nic.Info.HwAddress = "60-a3-e3-de-e0-54"
        devicemgmt = AsyncMock()
        devicemgmt.GetNetworkInterfaces = AsyncMock(return_value=[nic])
        info = MagicMock()
        info.HardwareId = "AA:BB:CC:DD:EE:FF"  # 启用网卡没给,降级到 HardwareId
        info.SerialNumber = "12345"
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        with patch("onvif.ONVIFCamera", return_value=self._make_cam(devicemgmt)):
            result = await svc.read_device_hardware_id("192.168.4.16", 80, "admin", "pass")
        assert result == "AA:BB:CC:DD:EE:FF"

    @pytest.mark.asyncio
    async def test_falls_back_to_hardware_id_when_no_nic(self):
        """GetNetworkInterfaces 没给 MAC 时,降级用像 MAC 的 HardwareId。"""
        svc = CameraDiscoveryService()
        devicemgmt = AsyncMock()
        devicemgmt.GetNetworkInterfaces = AsyncMock(return_value=[])
        info = MagicMock()
        info.HardwareId = "AA:BB:CC:DD:EE:FF"
        info.SerialNumber = "12345"
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        with patch("onvif.ONVIFCamera", return_value=self._make_cam(devicemgmt)):
            result = await svc.read_device_hardware_id("192.168.4.16", 80, "admin", "pass")
        assert result == "AA:BB:CC:DD:EE:FF"

    @pytest.mark.asyncio
    async def test_falls_back_to_serial_when_no_mac_anywhere(self):
        """哪儿都没 MAC 时,兜底返回 SerialNumber。"""
        svc = CameraDiscoveryService()
        devicemgmt = AsyncMock()
        devicemgmt.GetNetworkInterfaces = AsyncMock(return_value=[])
        info = MagicMock()
        info.HardwareId = ""  # 空
        info.SerialNumber = "TP-ABC123"
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        with patch("onvif.ONVIFCamera", return_value=self._make_cam(devicemgmt)):
            result = await svc.read_device_hardware_id("192.168.4.16", 80, "admin", "pass")
        assert result == "TP-ABC123"

    @pytest.mark.asyncio
    async def test_get_network_interfaces_error_falls_back(self):
        """GetNetworkInterfaces 报错时,不崩,降级到 DeviceInformation。"""
        svc = CameraDiscoveryService()
        devicemgmt = AsyncMock()
        devicemgmt.GetNetworkInterfaces = AsyncMock(side_effect=RuntimeError("not supported"))
        info = MagicMock()
        info.HardwareId = "AA:BB:CC:DD:EE:FF"
        info.SerialNumber = "12345"
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        with patch("onvif.ONVIFCamera", return_value=self._make_cam(devicemgmt)):
            result = await svc.read_device_hardware_id("192.168.4.16", 80, "admin", "pass")
        assert result == "AA:BB:CC:DD:EE:FF"

    @pytest.mark.asyncio
    async def test_empty_ip_raises(self):
        svc = CameraDiscoveryService()
        with pytest.raises(ValueError):
            await svc.read_device_hardware_id("", 80, "admin", "pass")


class TestInferSubnet:
    """从旧 IP 推 /24 子网。"""

    def test_normal_ip(self):
        assert infer_subnet("192.168.4.38") == "192.168.4.0/24"

    def test_different_octet(self):
        assert infer_subnet("10.0.1.5") == "10.0.1.0/24"

    def test_invalid_returns_empty(self):
        assert infer_subnet("not-an-ip") == ""

    def test_empty_returns_empty(self):
        assert infer_subnet("") == ""


class TestMacMatch:
    """MAC 匹配(归一化后比较)。"""

    def test_match_different_format(self):
        svc = CameraDiscoveryService()
        assert svc._mac_match("AA:BB:CC:DD:EE:FF", "aabbccddeeff") is True

    def test_match_dashes(self):
        svc = CameraDiscoveryService()
        assert svc._mac_match("aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF") is True

    def test_no_match(self):
        svc = CameraDiscoveryService()
        assert svc._mac_match("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66") is False

    def test_empty_target_no_match(self):
        svc = CameraDiscoveryService()
        assert svc._mac_match("aa:bb:cc:dd:ee:ff", "") is False


class TestScanPorts:
    """_scan_ports: mock socket,并发端口探测返回开放 IP 列表。"""

    @pytest.mark.asyncio
    async def test_returns_open_ips(self):
        svc = CameraDiscoveryService()

        def fake_check(ip, port, timeout):
            # 只 192.168.1.50:80 开放
            return ip == "192.168.1.50" and port == 80

        with patch.object(svc, "_check_port_open", side_effect=fake_check):
            result = await svc._scan_ports(["192.168.1.49", "192.168.1.50", "192.168.1.51"])
        assert "192.168.1.50" in result
        assert "192.168.1.49" not in result

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        svc = CameraDiscoveryService()
        result = await svc._scan_ports([])
        assert result == []


class TestFindCamera:
    """find_camera: 两段式扫描整合 —— 端口探测 + ONVIF probe + MAC 匹配。"""

    @pytest.mark.asyncio
    async def test_finds_matching_mac(self):
        """192.168.1.50 端口开放且 MAC 匹配 → 返回该 IP。"""
        svc = CameraDiscoveryService()
        # 子网扫描返回两个端口开放的候选,只有 .50 的 MAC 匹配
        with patch.object(svc, "_scan_ports", AsyncMock(return_value=["192.168.1.49", "192.168.1.50"])), \
             patch.object(svc, "_probe_candidate", AsyncMock(side_effect=lambda ip: "aabbccddeeff" if ip == "192.168.1.50" else "")):
            found_ip = await svc.find_camera(target_mac="aabbccddeeff", subnet="192.168.1.0/24")
        assert found_ip == "192.168.1.50"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        """扫描到的候选 MAC 都不匹配 → 返回 None。

        用短 timeout + mock sleep 避免真睡(否则会跑满 config 默认 30s)。
        """
        svc = CameraDiscoveryService()
        with patch.object(svc, "_scan_ports", AsyncMock(return_value=["192.168.1.49"])), \
             patch.object(svc, "_probe_candidate", AsyncMock(return_value="112233445566")), \
             patch("app.services.camera_discovery_service.asyncio.sleep", AsyncMock()):
            found_ip = await svc.find_camera(
                target_mac="aabbccddeeff",
                subnet="192.168.1.0/24",
                timeout=0.01,
            )
        assert found_ip is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """超时未命中 → 返回 None,status=not_found。"""
        svc = CameraDiscoveryService()
        # 每轮扫描都不命中,且 _RESCAN_INTERVAL=5s 会拖过 1s 超时
        with patch.object(svc, "_scan_ports", AsyncMock(return_value=[])), \
             patch("app.services.camera_discovery_service.asyncio.sleep", AsyncMock()):
            found_ip = await svc.find_camera(
                target_mac="aabbccddeeff",
                subnet="192.168.1.0/24",
                timeout=0.01,
            )
        assert found_ip is None
        assert svc._status == "not_found"

    @pytest.mark.asyncio
    async def test_concurrent_find_skipped_when_lock_held(self):
        """锁已被占用时,find_camera 直接返回 None,不重复启动扫描。

        场景:worker 掉线触发 + 用户手动点发现按钮并发,或连点按钮。
        第二次发现应在锁忙时跳过,避免重复扫描 + 并发写 config。
        """
        svc = CameraDiscoveryService()
        # 手动占住锁,模拟另一路 find_camera 正在扫描
        await svc._discovery_lock.acquire()
        try:
            with patch.object(svc, "_scan_locked", AsyncMock()) as mock_scan, \
                 patch.object(svc, "_scan_ports", AsyncMock()) as mock_ports:
                result = await svc.find_camera(
                    target_mac="aabbccddeeff", subnet="192.168.1.0/24",
                )
            # 锁忙 → 直接返回 None,扫描逻辑根本没启动
            assert result is None
            mock_scan.assert_not_called()
            mock_ports.assert_not_called()
        finally:
            svc._discovery_lock.release()


class TestApplyFoundIp:
    """apply_found_ip: 更新 vision.rtsp_url(只换 IP)+ ptz.ip,通知 ptz 重连。"""

    @pytest.mark.asyncio
    async def test_updates_rtsp_url_and_ptz_ip(self):
        from app.core import config as cfg
        # 起始 config 有旧 rtsp_url 和 ptz.ip
        cfg.CONFIG["vision"]["rtsp_url"] = "rtsp://192.168.1.50:554/stream2"
        cfg.CONFIG["ptz"]["ip"] = "192.168.1.50"
        svc = CameraDiscoveryService()
        notify_mock = MagicMock()
        with patch("app.services.camera_discovery_service.ptz_service_notify_ip_changed", notify_mock):
            await svc.apply_found_ip("192.168.1.99")
        # rtsp_url 的 IP 被换,port/path/凭据保留
        assert "192.168.1.99" in cfg.CONFIG["vision"]["rtsp_url"]
        assert ":554/stream2" in cfg.CONFIG["vision"]["rtsp_url"]
        assert "192.168.1.50" not in cfg.CONFIG["vision"]["rtsp_url"]
        # ptz.ip 同步更新
        assert cfg.CONFIG["ptz"]["ip"] == "192.168.1.99"
        # ptz 收到重连通知
        notify_mock.assert_called_once_with("192.168.1.99")

    @pytest.mark.asyncio
    async def test_no_rtsp_url_only_updates_ptz(self):
        """USB 模式(无 rtsp_url)只更新 ptz.ip。"""
        from app.core import config as cfg
        cfg.CONFIG["vision"]["rtsp_url"] = ""
        cfg.CONFIG["ptz"]["ip"] = "192.168.1.50"
        svc = CameraDiscoveryService()
        with patch("app.services.camera_discovery_service.ptz_service_notify_ip_changed", MagicMock()):
            await svc.apply_found_ip("192.168.1.99")
        assert cfg.CONFIG["vision"]["rtsp_url"] == ""
        assert cfg.CONFIG["ptz"]["ip"] == "192.168.1.99"


class TestReplaceUrlHost:
    """_replace_url_host: 只换 host,保留端口/路径/凭据。"""

    def test_plain_url(self):
        svc = CameraDiscoveryService()
        out = svc._replace_url_host("rtsp://192.168.1.50:554/stream2", "192.168.1.99")
        assert out == "rtsp://192.168.1.99:554/stream2"

    def test_url_with_credentials(self):
        svc = CameraDiscoveryService()
        out = svc._replace_url_host("rtsp://admin:pass@192.168.1.50:554/stream2", "192.168.1.99")
        assert out == "rtsp://admin:pass@192.168.1.99:554/stream2"

    def test_url_no_port(self):
        svc = CameraDiscoveryService()
        out = svc._replace_url_host("rtsp://192.168.1.50/stream", "192.168.1.99")
        assert out == "rtsp://192.168.1.99/stream"


class TestCaptureMacOnStartup:
    """首次 MAC 捕获:有 IP 无 MAC 时用现有 IP 读 MAC 写回 config。"""

    @pytest.mark.asyncio
    async def test_captures_when_no_mac(self):
        from app.core import config as cfg
        cfg.CONFIG["vision"]["device_mac"] = ""
        cfg.CONFIG["ptz"]["ip"] = "192.168.1.50"
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock(return_value="aabbccddeeff")), \
             patch("app.services.camera_discovery_service.update_config_section") as uc:
            await svc.capture_mac_on_startup()
        uc.assert_called_once_with("vision", {"device_mac": "aabbccddeeff"})

    @pytest.mark.asyncio
    async def test_skips_when_mac_already_set(self):
        from app.core import config as cfg
        cfg.CONFIG["vision"]["device_mac"] = "aabbccddeeff"
        cfg.CONFIG["ptz"]["ip"] = "192.168.1.50"
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock()) as rd, \
             patch("app.services.camera_discovery_service.update_config_section") as uc:
            await svc.capture_mac_on_startup()
        rd.assert_not_called()
        uc.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_ip(self):
        from app.core import config as cfg
        cfg.CONFIG["vision"]["device_mac"] = ""
        cfg.CONFIG["ptz"]["ip"] = ""
        # rtsp_url 也需清空:capture 会从 rtsp_url 兜底提 IP
        cfg.CONFIG["vision"]["rtsp_url"] = ""
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock()) as rd:
            await svc.capture_mac_on_startup()
        rd.assert_not_called()

    @pytest.mark.asyncio
    async def test_capture_failure_does_not_raise(self):
        """读取失败(设备离线)不影响启动。"""
        from app.core import config as cfg
        cfg.CONFIG["vision"]["device_mac"] = ""
        cfg.CONFIG["ptz"]["ip"] = "192.168.1.50"
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock(side_effect=Exception("offline"))):
            # 不抛异常
            await svc.capture_mac_on_startup()


class TestDisabledAndEdgeCases:
    """discovery 关闭/无凭证等边界场景。"""

    @pytest.mark.asyncio
    async def test_find_and_apply_no_mac_returns_none(self):
        """无 MAC 时 find_and_apply 立即返回 None,不扫描。"""
        from app.core import config as cfg
        cfg.CONFIG["vision"]["device_mac"] = ""
        svc = CameraDiscoveryService()
        with patch.object(svc, "_scan_ports", AsyncMock()) as sp:
            result = await svc.find_and_apply(timeout=1)
        assert result is None
        sp.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_and_apply_applies_when_found(self):
        """find_and_apply: 找到 IP 后调 apply_found_ip。"""
        svc = CameraDiscoveryService()
        with patch.object(svc, "find_camera", AsyncMock(return_value="192.168.1.99")), \
             patch.object(svc, "apply_found_ip", AsyncMock()) as ap:
            result = await svc.find_and_apply(timeout=1)
        assert result == "192.168.1.99"
        ap.assert_called_once_with("192.168.1.99")

    @pytest.mark.asyncio
    async def test_find_and_apply_no_apply_when_not_found(self):
        """find_and_apply: 没找到时不调 apply_found_ip。"""
        svc = CameraDiscoveryService()
        with patch.object(svc, "find_camera", AsyncMock(return_value=None)), \
             patch.object(svc, "apply_found_ip", AsyncMock()) as ap:
            result = await svc.find_and_apply(timeout=1)
        assert result is None
        ap.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_candidate_no_credentials_returns_empty(self):
        """无 ONVIF 凭证时 _probe_candidate 返回空,跳过该候选。"""
        from app.core import config as cfg
        cfg.CONFIG["ptz"]["username"] = ""
        svc = CameraDiscoveryService()
        with patch.object(svc, "read_device_hardware_id", AsyncMock()) as rd:
            result = await svc._probe_candidate("192.168.1.50")
        assert result == ""
        rd.assert_not_called()
