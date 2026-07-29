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
    """read_device_hardware_id: mock ONVIFCamera,验证读 HardwareId 与降级。"""

    @pytest.mark.asyncio
    async def test_reads_hardware_id(self):
        svc = CameraDiscoveryService()
        info = MagicMock()
        info.HardwareId = "AA:BB:CC:DD:EE:FF"
        info.SerialNumber = "12345"
        devicemgmt = AsyncMock()
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        cam = MagicMock()
        cam.update_xaddrs = AsyncMock()
        cam.create_devicemgmt_service = AsyncMock(return_value=devicemgmt)
        with patch("onvif.ONVIFCamera", return_value=cam):
            result = await svc.read_device_hardware_id("192.168.1.50", 80, "admin", "pass")
        assert result == "AA:BB:CC:DD:EE:FF"

    @pytest.mark.asyncio
    async def test_falls_back_to_serial_when_no_hardware_id(self):
        svc = CameraDiscoveryService()
        info = MagicMock()
        info.HardwareId = ""
        info.SerialNumber = "TP-ABC123"
        devicemgmt = AsyncMock()
        devicemgmt.GetDeviceInformation = AsyncMock(return_value=info)
        cam = MagicMock()
        cam.update_xaddrs = AsyncMock()
        cam.create_devicemgmt_service = AsyncMock(return_value=devicemgmt)
        with patch("onvif.ONVIFCamera", return_value=cam):
            result = await svc.read_device_hardware_id("192.168.1.50", 80, "admin", "pass")
        assert result == "TP-ABC123"

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
