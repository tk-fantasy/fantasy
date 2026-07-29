"""ONVIF 摄像头自动发现服务 — DHCP 换 IP 后按 MAC 找回设备。

设计参考: docs/superpowers/specs/2026-07-29-onvif-camera-discovery-design.md

核心思路(方案 A —— 子网单播扫描,适配 Docker):
- WS-Discovery 多播在 Docker 桥接网络下被 NAT 丢弃,不能用。
- 改对子网内每个 IP 做单播 TCP 端口探测,再对端口开放的候选做 ONVIF probe
  读 HardwareId(MAC),MAC 匹配的即目标设备。
- 摄像头身份用 MAC(不变),config 只存 MAC 不存死 IP。

本服务无状态、被动调用:CameraStream._worker 掉线时调 find_camera(),
不自己跑后台线程,不持有摄像头连接。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from typing import Any

from ..core.config import get_config, update_config_section

logger = logging.getLogger(__name__)


def normalize_mac(mac: str) -> str:
    """归一化 MAC 为纯小写十六进制(去冒号/横线)。

    AA:BB:CC:DD:EE:FF / aa-bb-cc-dd-ee-ff / aabbccddeeff → aabbccddeeff
    空串 → 空串。
    """
    return re.sub(r"[^0-9a-fA-F]", "", mac or "").lower()


def infer_subnet(old_ip: str) -> str:
    """从旧 IP 推断 /24 子网(CIDR),如 192.168.4.38 → 192.168.4.0/24。

    无效 IP → 空串。
    """
    try:
        addr = ipaddress.ip_address(old_ip.strip())
    except ValueError:
        return ""
    network = ipaddress.ip_network(f"{addr}/24", strict=False)
    return str(network)


class CameraDiscoveryService:
    """ONVIF 设备发现。无状态,被动调用。"""

    # Stage1 端口探测并发上限(单次扫描 254 IP 用)
    _PORT_SCAN_CONCURRENCY = 150
    # Stage1 单个端口探测超时(秒)——空闲 IP 必须快速失败
    _PORT_PROBE_TIMEOUT = 0.5
    # 探测的端口列表:TP-Link 一般 80(ONVIF)+ 554(RTSP)
    _PROBE_PORTS = (80, 554)
    # 单次完整扫描后的退避间隔(未命中时)
    _RESCAN_INTERVAL = 5.0

    def __init__(self) -> None:
        # 发现过程中的状态(供前端查询)
        self._status: str = "idle"  # idle|scanning|found|not_found|disabled|error
        self._last_found_ip: str = ""
        self._last_error: str = ""

    @property
    def status(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "last_found_ip": self._last_found_ip,
            "last_error": self._last_error,
            "device_mac": str(get_config("vision.device_mac", "") or ""),
        }

    async def read_device_hardware_id(
        self, ip: str, port: int, user: str, pwd: str
    ) -> str:
        """单点连一台 ONVIF 设备,读 HardwareId(MAC)。

        HardwareId 为空或不像 MAC 时降级返回 SerialNumber。
        连接失败抛异常(由调用方决定如何处理)。
        """
        ip = (ip or "").strip()
        if not ip:
            raise ValueError("ip 不能为空")
        try:
            import onvif
            from onvif import ONVIFCamera
        except ImportError as e:
            raise RuntimeError("ONVIF 库未安装") from e

        wsdl_dir = os.path.join(os.path.dirname(onvif.__file__), "wsdl")
        cam = ONVIFCamera(ip, port, user, pwd, wsdl_dir=wsdl_dir)
        try:
            await cam.update_xaddrs()
            devicemgmt = await cam.create_devicemgmt_service()
            info = await devicemgmt.GetDeviceInformation()
        finally:
            # devicemgmt 是临时探测连接,不复用
            pass

        # HardwareId 通常是 MAC(如 "A0:BD:1D:..."),某型号为空则降级序列号
        hardware_id = str(getattr(info, "HardwareId", "") or "").strip()
        if hardware_id and normalize_mac(hardware_id):
            return hardware_id
        serial = str(getattr(info, "SerialNumber", "") or "").strip()
        return serial


# 模块级单例(无状态)。bootstrap / 路由直接 import 用。
discovery_service = CameraDiscoveryService()
