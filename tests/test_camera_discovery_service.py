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
