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

    def _mac_match(self, found_id: str, target_mac: str) -> bool:
        """比较两个硬件 ID 是否同一个 MAC(归一化后)。target 为空则不匹配。"""
        target = normalize_mac(target_mac)
        if not target:
            return False
        return normalize_mac(found_id) == target

    @staticmethod
    def _check_port_open(ip: str, port: int, timeout: float) -> bool:
        """同步 TCP 端口探测。开放返回 True,否则 False(超时/拒绝)。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((ip, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False
        finally:
            sock.close()

    async def _scan_ports(self, ips: list[str]) -> list[str]:
        """Stage1: 并发对 IP 列表探测端口,返回任一探测端口开放的 IP 列表。

        用 asyncio + run_in_executor 跑同步 socket,限制并发避免 fd 耗尽。
        """
        if not ips:
            return []

        semaphore = asyncio.Semaphore(self._PORT_SCAN_CONCURRENCY)
        loop = asyncio.get_running_loop()

        async def _check_one(ip: str) -> str | None:
            async with semaphore:
                for port in self._PROBE_PORTS:
                    is_open = await loop.run_in_executor(
                        None, self._check_port_open, ip, port, self._PORT_PROBE_TIMEOUT
                    )
                    if is_open:
                        return ip
                return None

        results = await asyncio.gather(*(_check_one(ip) for ip in ips), return_exceptions=True)
        return [r for r in results if isinstance(r, str)]

    async def _probe_candidate(self, ip: str) -> str:
        """Stage2: 对单个候选 IP 做 ONVIF probe,读 HardwareId。

        用 ptz 段的凭证(ONVIF 鉴权)和端口。失败返回空串(不抛,扫描继续)。
        """
        port = int(get_config("ptz.port", 80))
        user = str(get_config("ptz.username", ""))
        pwd_env = str(get_config("ptz.password_env", ""))
        pwd = os.getenv(pwd_env, "") if pwd_env else ""
        if not user or not pwd:
            return ""
        try:
            return await asyncio.wait_for(
                self.read_device_hardware_id(ip, port, user, pwd),
                timeout=3.0,
            )
        except (asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
            logger.debug("ONVIF probe failed for %s: %s", ip, e)
            return ""

    @staticmethod
    def _list_subnet_ips(subnet_cidr: str) -> list[str]:
        """展开 /24 子网为可用的主机 IP 列表(去网络号和广播)。"""
        try:
            net = ipaddress.ip_network(subnet_cidr, strict=False)
        except ValueError:
            return []
        # 跳过网络地址和广播地址(/24 下首尾两个)
        return [str(host) for host in net.hosts()]

    async def find_camera(
        self,
        target_mac: str | None = None,
        subnet: str | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """两段式扫描找目标设备当前 IP。

        Args:
            target_mac: 目标 MAC(归一化前任意格式),None 则读 config。
            subnet: 子网 CIDR,None 则从 config 旧 IP 推断。
            timeout: 总超时秒,None 则读 config discovery_timeout_seconds(默认 30)。

        Returns: 找到的 IP,或 None(超时/无 MAC/无子网)。
        """
        if target_mac is None:
            target_mac = str(get_config("vision.device_mac", "") or "")
        if not normalize_mac(target_mac):
            self._status = "error"
            self._last_error = "无设备 MAC,无法匹配"
            logger.warning("find_camera: no device_mac configured")
            return None
        if subnet is None:
            subnet = str(get_config("vision.discovery_subnet", "") or "").strip()
        if not subnet:
            # 从 ptz.ip 或 rtsp_url 旧 IP 推断子网
            old_ip = str(get_config("ptz.ip", "") or "").strip()
            if not old_ip:
                old_ip = self._extract_ip_from_rtsp_url(
                    str(get_config("vision.rtsp_url", "") or "")
                )
            subnet = infer_subnet(old_ip)
        if not subnet:
            self._status = "error"
            self._last_error = "无法推断子网"
            return None
        if timeout is None:
            timeout = float(get_config("vision.discovery_timeout_seconds", 30))

        ips = self._list_subnet_ips(subnet)
        if not ips:
            self._status = "error"
            self._last_error = f"子网 {subnet} 无可用 IP"
            return None

        logger.info(
            "find_camera: scanning %s (%d IPs) for MAC %s, timeout=%.0fs",
            subnet, len(ips), normalize_mac(target_mac), timeout,
        )
        self._status = "scanning"
        deadline = asyncio.get_running_loop().time() + timeout

        while asyncio.get_running_loop().time() < deadline:
            candidates = await self._scan_ports(ips)
            for ip in candidates:
                hardware_id = await self._probe_candidate(ip)
                if self._mac_match(hardware_id, target_mac):
                    self._status = "found"
                    self._last_found_ip = ip
                    self._last_error = ""
                    logger.info("find_camera: matched MAC at %s", ip)
                    return ip
            # 本轮未命中,退避后重扫
            await asyncio.sleep(self._RESCAN_INTERVAL)

        self._status = "not_found"
        self._last_error = f"在 {subnet} 内未找到 MAC {normalize_mac(target_mac)} 的设备"
        logger.warning("find_camera: not found within %.0fs", timeout)
        return None

    @staticmethod
    def _extract_ip_from_rtsp_url(url: str) -> str:
        """从 rtsp URL 提 host IP。复用 ptz_service.extract_host_from_url。"""
        from .ptz_service import extract_host_from_url
        return extract_host_from_url(url)


# 模块级单例(无状态)。bootstrap / 路由直接 import 用。
discovery_service = CameraDiscoveryService()
