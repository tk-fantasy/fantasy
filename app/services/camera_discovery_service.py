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


def ptz_service_notify_ip_changed(new_ip: str) -> None:
    """薄包装:通知 ptz_service IP 变了。单独成函数便于测试 mock。"""
    from .ptz_service import ptz_service
    ptz_service.notify_ip_changed(new_ip)


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
        # Task 3:多路化 —— db 用于读 cameras 行(MAC/子网/凭证);
        # _on_ip_changed 取代旧的硬接线 ptz_service_notify_ip_changed。
        # 单例在 import 时建(database.py:397),那时 Database 还没 init,
        # 故 _db 初始 None,由 bootstrap 在 Database.init() 后 set_db 注入。
        self._db: Any = None
        self._on_ip_changed: Any = None
        # find_camera 多路时缓存该路凭证供 _probe_candidate 用;None=走旧 config 路径
        self._probe_creds: tuple | None = None
        # 并发保护:find_camera 扫描期间,后续触发(worker+手动、连点按钮)直接跳过,
        # 不重复启动扫描轮,也不并发写 config + 通知 PTZ。非阻塞——已在扫就返回当前状态。
        self._discovery_lock = asyncio.Lock()

    def set_db(self, db) -> None:
        """bootstrap 顺序兜底:db 在 Database.init() 后才有,允许后注入。"""
        self._db = db

    def set_on_ip_changed(self, callback) -> None:
        """注册 IP 变现回调(由 CameraManager 注入,负责该路 stream/ptz 重连)。

        取代旧硬接线 ptz_service_notify_ip_changed —— 多路时代每路 stream/ptz
        各自重连,不能写死全局 ptz_service 单例。
        """
        self._on_ip_changed = callback

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
        """单点连一台 ONVIF 设备,读可作身份证的硬件标识(优先 MAC)。

        取值优先级(实测不同厂商字段差异很大):
          1. GetNetworkInterfaces[].Info.HwAddress —— 真正的 MAC,最可靠。
             TP-Link TL-IPC43CL-V2 的 HardwareId 返回的是硬件版本号 "2.0"
             (不是 MAC),SerialNumber 只返回 MAC 尾 4 字节,只有这里能给
             完整 MAC。海康/大华一般也能从这里拿到。
          2. GetDeviceInformation.HardwareId —— 部分厂商这里是 MAC。
             只当它长得像 MAC(归一化后 12 位 hex)才采用。
          3. GetDeviceInformation.SerialNumber —— 兜底(非 MAC 但唯一)。

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

            # 优先级 1: GetNetworkInterfaces 的 HwAddress(真 MAC)
            try:
                nics = await devicemgmt.GetNetworkInterfaces()
                for nic in nics or []:
                    nic_info = getattr(nic, "Info", None)
                    if nic_info is None:
                        continue
                    # 只认启用的网卡,避免拿到未用的虚拟接口
                    if getattr(nic, "Enabled", True) is False:
                        continue
                    hw = str(getattr(nic_info, "HwAddress", "") or "").strip()
                    if normalize_mac(hw):
                        return hw
            except Exception:  # noqa: BLE001
                logger.debug("GetNetworkInterfaces failed, falling back to DeviceInformation", exc_info=True)

            info = await devicemgmt.GetDeviceInformation()
        finally:
            # 显式关闭 ONVIF transport(zeep/aiohttp 连接),不依赖 GC 回收。
            # 探测候选设备时一轮可能连 1-5 台,不关会累积连接耗尽 fd。
            try:
                await cam.close()
            except Exception:  # noqa: BLE001
                logger.debug("ONVIFCamera close failed", exc_info=True)

        # 优先级 2: HardwareId(仅当长得像 MAC)
        hardware_id = str(getattr(info, "HardwareId", "") or "").strip()
        if hardware_id and normalize_mac(hardware_id):
            return hardware_id
        # 优先级 3: SerialNumber(兜底,非 MAC 但唯一)
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

        Task 3:多路时用 find_camera 缓存的 _probe_creds(从 cameras 行读的
        per-camera 凭证);旧路径(单摄/_probe_creds=None)走 ptz config。
        失败返回空串(不抛,扫描继续)。
        """
        if self._probe_creds is not None:
            port, user, pwd = self._probe_creds
        else:
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
        except Exception as e:  # noqa: BLE001
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
        camera_id: str = "",
        target_mac: str | None = None,
        subnet: str | None = None,
        timeout: float | None = None,
    ) -> str | None:
        """两段式扫描找目标设备当前 IP。

        Task 3 多路化:优先按 camera_id 从 db.cameras_get 行读 MAC/子网/
        discovery_enabled/凭证;camera_id 为空时回退旧逻辑(从 target_mac/
        subnet 参数或全局 config 读),向后兼容旧测试与未迁移场景。

        Args:
            camera_id: 摄像头 id(多路);非空则从 cameras 行读配置。
            target_mac: 目标 MAC(camera_id 空时用),None 则读 config。
            subnet: 子网 CIDR(camera_id 空时用),None 则从 config 旧 IP 推断。
            timeout: 总超时秒,None 则读 config discovery_timeout_seconds(默认 30)。

        Returns: 找到的 IP,或 None(超时/无 MAC/无子网/discovery 关闭/db 未注入)。
        """
        # —— 多路:按 camera_id 从 cameras 行读 ——
        if camera_id:
            if self._db is None:
                return None
            row = await self._db.cameras_get(camera_id)
            if not row or not row.get("discovery_enabled", 1):
                return None
            target_mac = str(row.get("device_mac", "") or "")
            subnet = str(row.get("discovery_subnet", "") or "").strip() or None
            if not subnet:
                # 从行内 ptz_ip 或 rtsp_url 推断子网
                old_ip = str(row.get("ptz_ip", "") or "").strip()
                if not old_ip:
                    old_ip = self._extract_ip_from_rtsp_url(
                        str(row.get("rtsp_url", "") or ""))
                subnet = infer_subnet(old_ip)
            if timeout is None:
                timeout = float(get_config("vision.discovery_timeout_seconds", 30))
            # 缓存凭证供 _probe_candidate 用(走 per-camera 路径)
            self._probe_creds = (
                int(row.get("ptz_port", 80)),
                str(row.get("ptz_username", "")),
                str(row.get("ptz_password", "")),
            )
        else:
            # —— 旧逻辑:从参数/config 读(向后兼容)——
            if target_mac is None:
                target_mac = str(get_config("vision.device_mac", "") or "")
            if subnet is None:
                subnet = str(get_config("vision.discovery_subnet", "") or "").strip()
            if not subnet:
                old_ip = str(get_config("ptz.ip", "") or "").strip()
                if not old_ip:
                    old_ip = self._extract_ip_from_rtsp_url(
                        str(get_config("vision.rtsp_url", "") or "")
                    )
                subnet = infer_subnet(old_ip)
            if timeout is None:
                timeout = float(get_config("vision.discovery_timeout_seconds", 30))
            self._probe_creds = None   # _probe_candidate 走旧 config 路径

        if not normalize_mac(target_mac or ""):
            self._status = "error"
            self._last_error = "无设备 MAC,无法匹配"
            logger.warning("find_camera: no device_mac configured")
            return None
        if not subnet:
            self._status = "error"
            self._last_error = "无法推断子网"
            return None

        # 并发保护:已有扫描在进行(worker 触发 + 手动按钮,或连点),不重复启动。
        # 非阻塞——拿不到锁直接返回,调用方(worker)照常退避,手动按钮可从 status 看到 scanning。
        if self._discovery_lock.locked():
            logger.info("find_camera: scan already in progress, skipping")
            return None
        async with self._discovery_lock:
            return await self._scan_locked(target_mac, subnet, timeout)

    async def _scan_locked(self, target_mac: str, subnet: str, timeout: float) -> str | None:
        """实际扫描逻辑(find_camera 持锁后调用,假设已独占 _discovery_lock)。"""
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

    async def apply_found_ip(self, camera_id: str = "", new_ip: str = "") -> None:
        """发现到新 IP 后,更新该路配置 + 通知重连。

        Task 3 多路化:
        - camera_id 非空 → 更新该路 cameras 行(ptz_ip + rtsp_url 换 host),
          触发 _on_ip_changed(camera_id, new_ip) 回调(取代旧硬接线
          ptz_service_notify_ip_changed)。
        - camera_id 空 → 旧逻辑(写 config.json + ptz_service_notify_ip_changed),
          向后兼容未迁移场景。

        rtsp_url 只替换 host 部分,保留端口/路径/凭据;USB 模式(无 rtsp_url)
        只更新 ptz_ip。
        """
        new_ip = (new_ip or "").strip()
        if not new_ip:
            logger.warning("apply_found_ip: empty ip, skip")
            return

        if camera_id and self._db is not None:
            # —— 多路:更新 cameras 行 ——
            row = await self._db.cameras_get(camera_id)
            if not row:
                return
            old_rtsp = str(row.get("rtsp_url", "") or "").strip()
            fields: dict = {"ptz_ip": new_ip}
            if old_rtsp:
                fields["rtsp_url"] = self._replace_url_host(old_rtsp, new_ip)
            await self._db.cameras_update(camera_id, fields)
            logger.info("apply_found_ip: camera %s updated to %s", camera_id, new_ip)
            if self._on_ip_changed is not None:
                try:
                    self._on_ip_changed(camera_id, new_ip)
                except Exception:  # noqa: BLE001
                    logger.exception("on_ip_changed callback failed for %s", camera_id)
            return

        # —— 旧逻辑:写 config.json + 通知全局 ptz 单例(向后兼容)——
        update_config_section("ptz", {"ip": new_ip})
        logger.info("apply_found_ip: ptz.ip updated to %s", new_ip)
        old_url = str(get_config("vision.rtsp_url", "") or "").strip()
        if old_url:
            new_url = self._replace_url_host(old_url, new_ip)
            update_config_section("vision", {"rtsp_url": new_url})
            logger.info("apply_found_ip: rtsp_url host updated to %s", new_ip)
        try:
            ptz_service_notify_ip_changed(new_ip)
        except Exception:  # noqa: BLE001
            logger.exception("notify ptz ip change failed")

    @staticmethod
    def _replace_url_host(url: str, new_host: str) -> str:
        """替换 URL 里的 host(保留 scheme/凭据/端口/路径)。

        rtsp://admin:pwd@192.168.1.50:554/stream2 → ...@192.168.1.99:554/stream2
        rtsp://192.168.1.50:554/stream2          → rtsp://192.168.1.99:554/stream2
        """
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        # netloc = [user:pass@]host[:port],只替换 host 部分
        netloc = parsed.netloc
        if "@" in netloc:
            creds, hostport = netloc.rsplit("@", 1)
            creds = creds + "@"
        else:
            creds, hostport = "", netloc
        if ":" in hostport:
            _, port = hostport.split(":", 1)
            new_netloc = f"{creds}{new_host}:{port}"
        else:
            new_netloc = f"{creds}{new_host}"
        return urlunparse(parsed._replace(netloc=new_netloc))

    async def capture_mac_on_startup(self, camera_id: str = "") -> None:
        """首次 MAC 捕获:有 IP 无 MAC 时,用现有 IP 读一次 MAC 写回。

        Task 3 多路化:camera_id 非空 → 从 cameras 行读 IP/凭证,MAC 写回
        该路 cameras 行;camera_id 空 → 旧逻辑(读/写 config.json)。

        在 bootstrap 启动时调用(后台遍历各路,不阻塞启动)。失败不影响启动
        —— 设备离线时下次掉线会 fallback 到子网全扫。
        """
        if camera_id and self._db is not None:
            # —— 多路:从 cameras 行读,写回 cameras 行 ——
            row = await self._db.cameras_get(camera_id)
            if not row or not row.get("discovery_enabled", 1):
                return
            if normalize_mac(str(row.get("device_mac", "") or "")):
                logger.info("capture_mac: cam %s device_mac already set, skip", camera_id)
                return
            ip = str(row.get("ptz_ip", "") or "").strip()
            if not ip:
                ip = self._extract_ip_from_rtsp_url(str(row.get("rtsp_url", "") or ""))
            if not ip:
                logger.info("capture_mac: cam %s no known IP, skip", camera_id)
                return
            port = int(row.get("ptz_port", 80))
            user = str(row.get("ptz_username", ""))
            pwd = str(row.get("ptz_password", ""))
            if not user or not pwd:
                logger.info("capture_mac: cam %s no ONVIF credentials, skip", camera_id)
                return
            try:
                hardware_id = await asyncio.wait_for(
                    self.read_device_hardware_id(ip, port, user, pwd), timeout=8.0)
            except Exception as e:  # noqa: BLE001
                logger.warning("capture_mac: cam %s failed: %s (non-fatal)", camera_id, e)
                return
            if hardware_id:
                await self._db.cameras_update(camera_id, {"device_mac": hardware_id})
                logger.info("capture_mac: cam %s stored device_mac=%s", camera_id, hardware_id)
            else:
                logger.warning("capture_mac: cam %s empty hardware id", camera_id)
            return

        # —— 旧逻辑:读/写 config.json(向后兼容)——
        if not bool(get_config("vision.discovery_enabled", False)):
            return
        existing_mac = str(get_config("vision.device_mac", "") or "").strip()
        if normalize_mac(existing_mac):
            logger.info("capture_mac: device_mac already set (%s), skip", existing_mac)
            return
        # 现有 IP:优先 ptz.ip,其次 rtsp_url
        ip = str(get_config("ptz.ip", "") or "").strip()
        if not ip:
            ip = self._extract_ip_from_rtsp_url(str(get_config("vision.rtsp_url", "") or ""))
        if not ip:
            logger.info("capture_mac: no known IP, skip (will full-scan on disconnect)")
            return
        port = int(get_config("ptz.port", 80))
        user = str(get_config("ptz.username", ""))
        pwd_env = str(get_config("ptz.password_env", ""))
        pwd = os.getenv(pwd_env, "") if pwd_env else ""
        if not user or not pwd:
            logger.info("capture_mac: no ONVIF credentials, skip")
            return
        try:
            hardware_id = await asyncio.wait_for(
                self.read_device_hardware_id(ip, port, user, pwd),
                timeout=8.0,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("capture_mac: failed to read from %s: %s (non-fatal)", ip, e)
            return
        if hardware_id:
            update_config_section("vision", {"device_mac": hardware_id})
            logger.info("capture_mac: stored device_mac=%s from %s", hardware_id, ip)
        else:
            logger.warning("capture_mac: device returned empty hardware id at %s", ip)

    async def find_and_apply(self, camera_id: str = "", timeout: float | None = None) -> str | None:
        """顶层编排:find_camera → apply_found_ip。返回找到的 IP 或 None。

        Task 3:camera_id 透传给 find_camera/apply_found_ip,支持多路。
        供 worker 掉线触发(camera_stream.py 调 find_and_apply(self.camera_id))
        和手动发现按钮共用。
        """
        found_ip = await self.find_camera(camera_id=camera_id, timeout=timeout)
        if found_ip:
            await self.apply_found_ip(camera_id=camera_id, new_ip=found_ip)
        return found_ip


# 模块级单例(无状态)。bootstrap / 路由直接 import 用。
discovery_service = CameraDiscoveryService()
