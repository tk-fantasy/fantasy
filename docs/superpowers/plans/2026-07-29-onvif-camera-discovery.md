# ONVIF 摄像头自动发现 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 摄像头 DHCP 换 IP 后,Aether 通过 ONVIF 子网单播扫描 + MAC 匹配自动找回新 IP,同步恢复 RTSP 画面和 PTZ 云台控制。

**Architecture:** 新增 `CameraDiscoveryService`(无状态、被动调用),被 `CameraStream._worker` 在连续开流失败时调用。发现到新 IP 后更新 `vision.rtsp_url` + `ptz.ip` 两处 config,并通知 ptz_service 重连。首次配对靠现有 IP 读 MAC 自动写入 config,失败时用户可手动填 IP 兜底。

**Tech Stack:** Python 3 + asyncio,`onvif-zeep-async>=4.0.0`(已装),`httpx`(已装),socket(端口探测),pytest + AsyncMock(测试)。

**参考设计:** `docs/superpowers/specs/2026-07-29-onvif-camera-discovery-design.md`

## Global Constraints

- 运行环境:Docker 容器,**禁止依赖 UDP 多播广播**(桥接网络默认丢),必须走子网单播 TCP。
- 身份证:MAC 地址(ONVIF `DeviceInformation.HardwareId`),不给 MAC 时降级 `SerialNumber`。
- 发现机制:方案 A —— 子网单播两段式扫描(Stage1 并发端口探测 → Stage2 ONVIF probe 读 MAC)。
- config 凭证分离:RTSP 密码 env = `RTSP_PASSWORD`,PTZ 密码 env = `PTZ_PASSWORD`(两套独立)。
- ONVIF 端口默认 80(ptz.port),用户名密码复用 ptz 段的 `username` / `password_env`。
- 复用现成函数:`config_probes.probe_ptz`、`ptz_service.extract_host_from_url`、`update_config_section`。
- 不接管 worker 重连循环:只在循环里加一个"找新 IP"的尝试;worker 仍是唯一持连接者。
- `discovery_enabled=false` 时行为与现状完全一致(向后兼容)。
- TDD:每步先写失败测试 → 验证失败 → 实现 → 验证通过 → commit。
- commit 用 `feat:`/`test:`/`refactor:` 前缀,中文描述。

---

## File Structure

| 文件 | 责任 | 操作 |
|------|------|------|
| `app/services/camera_discovery_service.py` | 发现服务核心:子网推断、端口探测、ONVIF probe、MAC 匹配、config 回写、PTZ 通知 | **新建** |
| `tests/test_camera_discovery_service.py` | 发现服务单测(子网推断、MAC 匹配、扫描、超时、config 回写) | **新建** |
| `tests/conftest.py` | test_config 补 `vision` / `ptz` 段(当前缺失) | **修改** |
| `app/services/ptz_service.py` | 加 `notify_ip_changed(new_ip)` 钩子 | **修改** |
| `app/camera_stream.py` | worker 连续开流失败时调 discovery(`_worker` 行 565-582 区域) | **修改** |
| `app/bootstrap.py` | 启动时首次 MAC 捕获钩子 | **修改** |
| `app/routes/discovery_routes.py` | 手动发现 + 手动填 IP 接口 | **新建** |
| `tests/test_discovery_routes.py` | 路由测试 | **新建** |
| `app/schema/api_schemas.py` | 新增 discovery 相关请求 schema | **修改** |
| `app/main.py` | 注册 discovery_routes 路由 | **修改** |
| `config.example.json` | 补 discovery 配置示例字段 | **修改** |

---

## Task 1: conftest 补 vision/ptz 段 + discovery 服务骨架与 MAC 读取

**Files:**
- Modify: `tests/conftest.py:40-64`(test_config dict)
- Create: `app/services/camera_discovery_service.py`
- Create: `tests/test_camera_discovery_service.py`

**Interfaces:**
- Consumes: `app.core.config.get_config`,`onvif.ONVIFCamera`,`os.getenv`
- Produces: `CameraDiscoveryService` 类,`async read_device_hardware_id(ip, port, user, pwd) -> str` 返回 HardwareId(MAC) 或 SerialNumber 降级;模块级单例 `discovery_service`。

**背景(实现者须知):** onvif-zeep-async 4.x 的 `ONVIFCamera` 构造和 `update_xaddrs` / `create_devicemgmt_service` 都是 async(内部 aiohttp)。devicemgmt 服务的 `GetDeviceInformation()` 在 4.x 也是 async。读取的 device info 对象有 `.HardwareId` 属性(TP-Link 通常是 MAC)。WSDL 路径有 bug,必须显式传 `wsdl_dir`(参考 `ptz_service.py:96` 和 `config_probes.py:310`)。

- [ ] **Step 1: 修改 conftest,补 vision/ptz 段**

在 `tests/conftest.py` 的 `test_config` dict 里(`"providers": {},` 之后,`"chat_assistant"` 之前),插入:

```python
        "vision": {
            "rtsp_url": "rtsp://192.168.1.50:554/stream2",
            "rtsp_username": "admin",
            "rtsp_password_env": "RTSP_PASSWORD",
            "device_mac": "",
            "discovery_enabled": True,
            "discovery_timeout_seconds": 30,
            "discovery_subnet": "",
        },
        "ptz": {
            "enabled": True,
            "ip": "192.168.1.50",
            "port": 80,
            "username": "admin",
            "password_env": "PTZ_PASSWORD",
            "speed": 0.5,
            "step_ms": 300,
        },
```

- [ ] **Step 2: 写失败测试 — read_device_hardware_id 读 MAC**

创建 `tests/test_camera_discovery_service.py`,先写 docstring 说明,再写第一个测试类:

```python
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
```

- [ ] **Step 3: 运行测试验证失败**

Run: `pytest tests/test_camera_discovery_service.py -v`
Expected: FAIL —— `ImportError: cannot import name 'CameraDiscoveryService'` / `infer_subnet` / `normalize_mac`

- [ ] **Step 4: 实现 normalize_mac + infer_subnet + read_device_hardware_id**

创建 `app/services/camera_discovery_service.py`:

```python
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
```

- [ ] **Step 5: 运行测试验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestNormalizeMac -v`
Expected: PASS(4 个测试全过)

- [ ] **Step 6: 写失败测试 — read_device_hardware_id mock ONVIF**

追加到 `tests/test_camera_discovery_service.py`:

```python
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
        with patch("app.services.camera_discovery_service.ONVIFCamera", return_value=cam) if hasattr(__import__("app.services.camera_discovery_service", fromlist=["ONVIFCamera"]), "ONVIFCamera") else patch("onvif.ONVIFCamera", return_value=cam):
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
        cam.create_devicemgmt_service = AsyncMock(return_value=cam)
        with patch("onvif.ONVIFCamera", return_value=cam):
            result = await svc.read_device_hardware_id("192.168.1.50", 80, "admin", "pass")
        assert result == "TP-ABC123"

    @pytest.mark.asyncio
    async def test_empty_ip_raises(self):
        svc = CameraDiscoveryService()
        with pytest.raises(ValueError):
            await svc.read_device_hardware_id("", 80, "admin", "pass")
```

> 注:`TestReadHardwareId` 里 patch 的是 `onvif.ONVIFCamera`(因为 `read_device_hardware_id` 内部 `from onvif import ONVIFCamera`)。第一个测试里那个复杂的 hasattr 三元是过渡写法 —— Step 4 实现会让它走 `patch("onvif.ONVIFCamera", ...)` 分支。**实现时把测试简化为统一用 `patch("onvif.ONVIFCamera", return_value=cam)`,删掉三元**,保持清晰。

- [ ] **Step 7: 简化测试 + 实现已就绪,运行验证**

把 `TestReadHardwareId.test_reads_hardware_id` 的 patch 行改为统一形式:

```python
        with patch("onvif.ONVIFCamera", return_value=cam):
            result = await svc.read_device_hardware_id("192.168.1.50", 80, "admin", "pass")
```

Run: `pytest tests/test_camera_discovery_service.py -v`
Expected: PASS(`TestNormalizeMac` 4 个 + `TestReadHardwareId` 3 个 = 7 个全过)

- [ ] **Step 8: commit**

```bash
git add tests/conftest.py app/services/camera_discovery_service.py tests/test_camera_discovery_service.py
git commit -m "feat: ONVIF 发现服务骨架 — MAC 归一化 + 单点硬件ID读取"
```

---

## Task 2: 子网推断 + 端口扫描 + ONVIF probe 找设备

**Files:**
- Modify: `app/services/camera_discovery_service.py`(加 `infer_subnet` 已在 Task1 加,本任务加 `_scan_ports` + `_probe_candidate` + `find_camera`)
- Test: `tests/test_camera_discovery_service.py`

**Interfaces:**
- Consumes: `normalize_mac`,`read_device_hardware_id`(Task1)
- Produces: `async find_camera(target_mac: str | None = None) -> str | None` 返回找到的 IP 或 None;内部用 `_scan_ports` / `_probe_candidate`

- [ ] **Step 1: 写失败测试 — infer_subnet**

追加到 `tests/test_camera_discovery_service.py`:

```python
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
```

- [ ] **Step 2: 运行验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestInferSubnet -v`
Expected: PASS(`infer_subnet` 已在 Task1 Step4 实现)

- [ ] **Step 3: 写失败测试 — MAC 匹配 helper + _scan_ports mock socket**

追加到测试文件:

```python
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
```

- [ ] **Step 4: 运行验证失败**

Run: `pytest tests/test_camera_discovery_service.py::TestMacMatch tests/test_camera_discovery_service.py::TestScanPorts -v`
Expected: FAIL —— `_mac_match` / `_scan_ports` / `_check_port_open` 未定义

- [ ] **Step 5: 实现 _mac_match + _check_port_open + _scan_ports**

在 `CameraDiscoveryService` 类里(Task1 的 `read_device_hardware_id` 之后)追加:

```python
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
```

- [ ] **Step 6: 运行验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestMacMatch tests/test_camera_discovery_service.py::TestScanPorts -v`
Expected: PASS

- [ ] **Step 7: 写失败测试 — find_camera 整合(命中)**

追加到测试文件:

```python
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
        """扫描到的候选 MAC 都不匹配 → 返回 None。"""
        svc = CameraDiscoveryService()
        with patch.object(svc, "_scan_ports", AsyncMock(return_value=["192.168.1.49"])), \
             patch.object(svc, "_probe_candidate", AsyncMock(return_value="112233445566")):
            found_ip = await svc.find_camera(target_mac="aabbccddeeff", subnet="192.168.1.0/24")
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
```

- [ ] **Step 8: 运行验证失败**

Run: `pytest tests/test_camera_discovery_service.py::TestFindCamera -v`
Expected: FAIL —— `find_camera` / `_probe_candidate` 未定义

- [ ] **Step 9: 实现 _probe_candidate + find_camera + _list_subnet_ips**

在 `CameraDiscoveryService` 类里追加:

```python
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
```

- [ ] **Step 10: 运行验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestFindCamera -v`
Expected: PASS(3 个全过)

- [ ] **Step 11: commit**

```bash
git add app/services/camera_discovery_service.py tests/test_camera_discovery_service.py
git commit -m "feat: ONVIF 两段式子网扫描 — 端口探测 + MAC 匹配找回设备 IP"
```

---

## Task 3: config 回写 + PTZ/IP 同步更新 + ptz_service 重连钩子

**Files:**
- Modify: `app/services/camera_discovery_service.py`(加 `apply_found_ip`)
- Modify: `app/services/ptz_service.py`(加 `notify_ip_changed`)
- Test: `tests/test_camera_discovery_service.py`(加 config 回写测试)
- Test: `tests/test_ptz_service.py`(加 notify_ip_changed 测试)

**Interfaces:**
- Consumes: `find_camera`(Task2),`update_config_section`,`ptz_service.notify_ip_changed`
- Produces: `async apply_found_ip(new_ip: str) -> None` —— 更新 vision.rtsp_url + ptz.ip,通知 ptz 重连

**背景:** config 里 RTSP 和 PTZ 是两套独立凭证(`RTSP_PASSWORD` / `PTZ_PASSWORD`),但指向同一台设备的同一 IP。发现到新 IP 只换 IP,保留端口/路径/凭证。ptz_service 是懒加载单例,`notify_ip_changed` 作废其缓存连接(`_broken=True`),下次动作时 `_ensure_connected` 自动用新 config 的 IP 重连。

- [ ] **Step 1: 写失败测试 — ptz_service.notify_ip_changed**

追加到 `tests/test_ptz_service.py`(末尾):

```python
class TestNotifyIpChanged:
    """notify_ip_changed: 作废缓存连接,下次 _ensure_connected 用新 config IP 重连。"""

    @pytest.mark.asyncio
    async def test_marks_broken_and_clears_connection(self):
        svc = PtzService()
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
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_ptz_service.py::TestNotifyIpChanged -v`
Expected: FAIL —— `notify_ip_changed` 不存在

- [ ] **Step 3: 实现 ptz_service.notify_ip_changed**

在 `app/services/ptz_service.py` 的 `PtzService` 类里(`_speed` 方法之后,`_continuous_move_locked` 之前)加:

```python
    def notify_ip_changed(self, new_ip: str) -> None:
        """通知 PTZ 摄像头 IP 已变(由 discovery 调用):作废缓存连接。

        config.ptz.ip 已被 discovery 更新过(写入内存 + 磁盘),这里只清掉
        旧 IP 建的 ONVIFCamera 缓存,下次 _ensure_connected 会读新 config 的 IP
        懒重连。同步操作,不需锁(只置标记和清引用,无 ONVIF 调用)。
        """
        logger.info("PTZ notified of IP change → %s, will reconnect on next call", new_ip)
        self._cam = None
        self._ptz = None
        self._profile_token = None
        self._broken = True
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_ptz_service.py::TestNotifyIpChanged -v`
Expected: PASS

- [ ] **Step 5: 写失败测试 — apply_found_ip 更新 config 双写**

追加到 `tests/test_camera_discovery_service.py`:

```python
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
```

- [ ] **Step 6: 运行验证失败**

Run: `pytest tests/test_camera_discovery_service.py::TestApplyFoundIp -v`
Expected: FAIL —— `apply_found_ip` / `ptz_service_notify_ip_changed` 未定义

- [ ] **Step 7: 实现 apply_found_ip**

在 `camera_discovery_service.py` 顶部 import 后,加一个轻量包装(便于测试 mock):

```python
def ptz_service_notify_ip_changed(new_ip: str) -> None:
    """薄包装:通知 ptz_service IP 变了。单独成函数便于测试 mock。"""
    from .ptz_service import ptz_service
    ptz_service.notify_ip_changed(new_ip)
```

然后在 `CameraDiscoveryService` 类里追加:

```python
    async def apply_found_ip(self, new_ip: str) -> None:
        """发现到新 IP 后,更新 vision.rtsp_url(只换 IP)+ ptz.ip,通知 PTZ 重连。

        rtsp_url 只替换 host 部分,保留端口/路径/凭据;USB 模式(无 rtsp_url)
        只更新 ptz.ip。两处都写 config.json(持久化)。
        """
        new_ip = (new_ip or "").strip()
        if not new_ip:
            logger.warning("apply_found_ip: empty ip, skip")
            return

        # 更新 ptz.ip
        update_config_section("ptz", {"ip": new_ip})
        logger.info("apply_found_ip: ptz.ip updated to %s", new_ip)

        # 更新 vision.rtsp_url(只换 host,保留端口/路径)
        old_url = str(get_config("vision.rtsp_url", "") or "").strip()
        if old_url:
            new_url = self._replace_url_host(old_url, new_ip)
            update_config_section("vision", {"rtsp_url": new_url})
            logger.info("apply_found_ip: rtsp_url host updated to %s", new_ip)

        # 通知 PTZ 重连
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
```

- [ ] **Step 8: 运行验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestApplyFoundIp tests/test_ptz_service.py::TestNotifyIpChanged -v`
Expected: PASS

- [ ] **Step 9: 补测试 — _replace_url_host 保留端口/路径/凭据**

追加到 `tests/test_camera_discovery_service.py`:

```python
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
```

- [ ] **Step 10: 运行验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestReplaceUrlHost -v`
Expected: PASS

- [ ] **Step 11: commit**

```bash
git add app/services/camera_discovery_service.py app/services/ptz_service.py tests/test_camera_discovery_service.py tests/test_ptz_service.py
git commit -m "feat: 发现新 IP 后同步更新 RTSP+PTZ config 并通知 PTZ 重连"
```

---

## Task 4: 首次 MAC 捕获(bootstrap)+ worker 掉线触发发现

**Files:**
- Modify: `app/services/camera_discovery_service.py`(加 `capture_mac_on_startup` + `find_and_apply` 顶层编排)
- Modify: `app/bootstrap.py`(启动钩子)
- Modify: `app/camera_stream.py`(`_worker` 行 565-582 区域加 discovery 触发)
- Test: `tests/test_camera_discovery_service.py`

**Interfaces:**
- Consumes: `find_camera`(Task2),`apply_found_ip`(Task3),`read_device_hardware_id`(Task1)
- Produces: `async capture_mac_on_startup() -> None`,`async find_and_apply() -> str | None`
- CameraStream 新增 `set_discovery_service(svc)` 注入;worker 用 `self._discovery_service`

**背景:** worker 重连循环(`_worker` 行 565-582)现在是"连续开流失败 → 指数退避 → 同一 rtsp_url 重试"。改成:失败达到一定次数且 discovery_enabled 时,先调 discovery 找新 IP,找到则 apply(下一轮 `_resolve_rtsp_url` 读到新值),没找到则照旧退避。bootstrap 启动时若有 IP 无 MAC,用现有 IP 读一次 MAC 写回 config。

- [ ] **Step 1: 写失败测试 — capture_mac_on_startup**

追加到 `tests/test_camera_discovery_service.py`:

```python
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
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_camera_discovery_service.py::TestCaptureMacOnStartup -v`
Expected: FAIL —— `capture_mac_on_startup` 未定义

- [ ] **Step 3: 实现 capture_mac_on_startup + find_and_apply**

在 `CameraDiscoveryService` 类里追加:

```python
    async def capture_mac_on_startup(self) -> None:
        """首次 MAC 捕获:有 IP 无 MAC 时,用现有 IP 读一次 MAC 写回 config。

        在 bootstrap 启动时调用(后台,不阻塞启动)。失败不影响启动
        —— 设备离线时下次掉线会 fallback 到子网全扫。
        """
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

    async def find_and_apply(self, timeout: float | None = None) -> str | None:
        """顶层编排:find_camera → apply_found_ip。返回找到的 IP 或 None。

        供 worker 掉线触发和手动发现按钮共用。
        """
        found_ip = await self.find_camera(timeout=timeout)
        if found_ip:
            await self.apply_found_ip(found_ip)
        return found_ip
```

- [ ] **Step 4: 运行验证通过**

Run: `pytest tests/test_camera_discovery_service.py::TestCaptureMacOnStartup -v`
Expected: PASS(4 个全过)

- [ ] **Step 5: 改 bootstrap —— 注入 discovery_service + 启动后台捕获 MAC**

在 `app/bootstrap.py` 顶部 import 区(其他 service import 之后)加:

```python
from .services.camera_discovery_service import discovery_service
```

在 `app/bootstrap.py` 的 `initialize_services()` 里,`return services` 之前(`services["ha_client_ref"]` 那行之后)加:

```python
    # ONVIF 摄像头自动发现(无状态,被动调用)
    services["discovery_service"] = discovery_service
```

- [ ] **Step 6: 改 main.py —— 启动后台 MAC 捕获**

在 `app/main.py` 的 `lifespan` 函数里,`camera_stream.start()`(行 505)之后,`_ha_catalog_refresh_loop` 任务(行 509)附近加:

```python
    # 后台捕获摄像头 MAC(首次配对,不阻塞启动)
    async def _startup_capture_mac():
        try:
            await discovery_service.capture_mac_on_startup()
        except Exception:
            logger.exception("startup MAC capture failed")

    _background_task_mgr.spawn(_startup_capture_mac(), name="capture_mac")
```

(具体变量名 `_background_task_mgr` / `discovery_service` 的引用见 main.py 现有上下文 —— 若 main.py 已有 `discovery_service = _services["discovery_service"]` 模式则对齐;没有则在 camera_stream 引用旁补一行。实现时先读 main.py 行 495-510 确认变量命名。)

- [ ] **Step 7: 改 camera_stream —— 注入 discovery + worker 掉线触发**

在 `app/camera_stream.py` 的 `CameraStream.__init__` 里(行 150 附近,`self._infer_futures: list = []` 之后)加:

```python
        # ONVIF 发现服务(掉线时找回 IP)。None 表示未注入,走纯指数退避。
        self._discovery_service: Any = None
        # 连续开流失败计数,达到阈值触发一次 discovery
        self._open_fail_count = 0
        self._discovery_trigger_threshold = 3
        # 上次 discovery 触发时间,限流避免短时间内重复扫描
        self._last_discovery_at = 0.0
        self._discovery_min_interval = 20.0
```

在 `set_event_loop` 方法之后加注入方法:

```python
    def set_discovery_service(self, svc: Any) -> None:
        """注入 ONVIF 发现服务(可选)。未注入则掉线走纯指数退避。"""
        self._discovery_service = svc
```

- [ ] **Step 8: 改 camera_stream._worker —— 掉线时触发 discovery**

读 `app/camera_stream.py` 行 565-582 的开流失败分支。当前是:

```python
                if self._cap is None or not self._cap.isOpened():
                    self._cap = self._open_camera()
                    if not self._cap.isOpened():
                        # 指数退避...
                        self._consecutive_open_failures += 1
                        backoff = ...
                        self._mark_camera_closed("无法打开电脑摄像头", keep_cache=True)
                        logger.error(...)
                        time.sleep(backoff)
                        continue
```

在 `time.sleep(backoff)` **之前**插入 discovery 触发逻辑:

```python
                        # 连续开流失败达到阈值,触发一次 ONVIF 发现找回 IP
                        # (限流:两次 discovery 至少间隔 _discovery_min_interval)
                        self._open_fail_count += 1
                        if (
                            self._discovery_service is not None
                            and bool(get_config("vision.discovery_enabled", False))
                            and self._open_fail_count >= self._discovery_trigger_threshold
                            and (time.time() - self._last_discovery_at) >= self._discovery_min_interval
                        ):
                            self._last_discovery_at = time.time()
                            self._open_fail_count = 0
                            self._mark_camera_closed("正在重新发现摄像头…", keep_cache=True)
                            logger.info("Triggering ONVIF discovery after %d open failures", self._discovery_trigger_threshold)
                            try:
                                import asyncio as _asyncio
                                # discovery 是 async,投到主循环跑
                                if self._loop and not self._loop.is_closed():
                                    fut = _asyncio.run_coroutine_threadsafe(
                                        self._discovery_service.find_and_apply(),
                                        self._loop,
                                    )
                                    fut.result(timeout=60)  # 等发现完成(上限 60s)
                            except Exception:
                                logger.exception("ONVIF discovery triggered from worker failed")
```

> 注:`get_config` 已在文件顶部 import(行 17 `from .core.config import get_config`)。worker 是线程,discovery 是 async,用 `run_coroutine_threadsafe` 投到主循环(与现有推理投递模式一致,见行 294)。find_and_apply 成功后 config 已更新,下一轮 `_open_camera` → `_resolve_rtsp_url` 会读到新 IP。

- [ ] **Step 9: 在 main.py lifespan 注入 discovery_service 到 camera_stream**

在 `app/main.py` 的 `camera_stream.set_event_loop(...)` 那行(行 502)附近加:

```python
    camera_stream.set_discovery_service(discovery_service)
```

(变量名 `discovery_service` 对齐 main.py 现有引用模式,实现时确认。)

- [ ] **Step 10: 运行全部发现服务测试**

Run: `pytest tests/test_camera_discovery_service.py tests/test_ptz_service.py -v`
Expected: PASS(全部)

- [ ] **Step 11: commit**

```bash
git add app/services/camera_discovery_service.py app/bootstrap.py app/main.py app/camera_stream.py tests/test_camera_discovery_service.py
git commit -m "feat: 启动首次 MAC 捕获 + worker 掉线自动触发 ONVIF 发现"
```

---

## Task 5: 手动发现 + 手动填 IP 路由 + schema + config 示例

**Files:**
- Modify: `app/schema/api_schemas.py`(加请求 schema)
- Create: `app/routes/discovery_routes.py`
- Create: `tests/test_discovery_routes.py`
- Modify: `app/main.py`(注册路由)
- Modify: `config.example.json`(补 discovery 字段)

**Interfaces:**
- Consumes: `discovery_service.find_and_apply`,`discovery_service.status`,`probe_ptz`,`update_config_section`
- Produces: `POST /discovery/find`(手动触发),`GET /discovery/status`,`POST /discovery/manual-ip`(手动填 IP)

- [ ] **Step 1: 写失败测试 — GET /discovery/status**

创建 `tests/test_discovery_routes.py`:

```python
"""Tests for app/routes/discovery_routes.py — 手动发现 + 手动填 IP。

discovery_service 单例 mock,验证路由的编排逻辑(status 查询、手动触发、
手动 IP 验证 + 写 config)。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """构造测试 app client,隔离 discovery_service。"""
    from app.main import app
    return TestClient(app)


class TestDiscoveryStatus:
    """GET /discovery/status: 返回发现服务状态。"""

    def test_status_returns_fields(self, client, monkeypatch):
        from app.services import camera_discovery_service as cds
        fake_status = {"status": "idle", "last_found_ip": "", "last_error": "", "device_mac": "aabbccddeeff"}
        monkeypatch.setattr(cds.discovery_service, "status", fake_status)
        with client:
            resp = client.get("/discovery/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["status"] == "idle"
        assert body["data"]["device_mac"] == "aabbccddeeff"
```

- [ ] **Step 2: 运行验证失败**

Run: `pytest tests/test_discovery_routes.py -v`
Expected: FAIL —— 路由未注册(404)

- [ ] **Step 3: 创建路由文件**

创建 `app/routes/discovery_routes.py`:

```python
"""ONVIF 摄像头发现路由 — 手动触发发现 + 手动填 IP 兜底。

discovery_service 是无状态单例,路由直接 import 用。
手动填 IP 时用 probe_ptz 验证凭证 + 写 config(同 advanced_routes 的模式)。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter

from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section
from ..schema.api_schemas import ManualIpRequest
from ..services.camera_discovery_service import discovery_service
from ..services.config_probes import probe_ptz

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/discovery/status")
async def get_discovery_status() -> ApiResponse[dict]:
    """查询发现服务当前状态(idle/scanning/found/not_found/error)。"""
    return ApiResponse(data=discovery_service.status)


@router.post("/discovery/find")
async def trigger_discovery() -> ApiResponse[dict]:
    """手动触发一次 ONVIF 发现。找到则自动更新 config 并返回新 IP。

    供前端「重新发现摄像头」按钮调用。超时由 config discovery_timeout_seconds 控制。
    """
    if not bool(get_config("vision.discovery_enabled", False)):
        return ApiResponse(
            code="disabled",
            message="自动发现未启用(在配置里开启 discovery_enabled)",
            data={"found": False},
        )
    found_ip = await discovery_service.find_and_apply()
    return ApiResponse(
        code="ok" if found_ip else "not_found",
        message=f"已发现并更新摄像头 IP: {found_ip}" if found_ip else discovery_service.status.get("last_error", "未找到设备"),
        data={"found": bool(found_ip), "ip": found_ip, **discovery_service.status},
    )


@router.post("/discovery/manual-ip")
async def set_manual_ip(payload: ManualIpRequest) -> ApiResponse[dict]:
    """手动填 IP 兜底:probe_ptz 验证 → 写 config(自动捕获/核对 MAC)。

    自动发现找不到时,用户手填 IP 救场。probe 通过才落盘。
    """
    ip = (payload.ip or "").strip()
    if not ip:
        return ApiResponse(code="bad_format", message="IP 不能为空", data={"saved": False})
    port = int(get_config("ptz.port", 80))
    user = str(get_config("ptz.username", ""))
    pwd_env = str(get_config("ptz.password_env", ""))
    pwd = os.getenv(pwd_env, "") if pwd_env else ""
    # 先 probe 验证凭证 + 可达性
    result = await probe_ptz(ip, port, user, pwd)
    if not result.ok:
        logger.warning("Manual IP probe rejected: %s (%s)", result.reason, result.detail)
        return ApiResponse(
            code="probe_failed",
            message=result.detail,
            data={"saved": False, **result.to_dict()},
        )
    # probe 通过 → 写 config(RTSP + PTZ 同步)
    await discovery_service.apply_found_ip(ip)
    # 顺便捕获/核对 MAC(手填 IP 后补上身份证)
    try:
        hardware_id = await discovery_service.read_device_hardware_id(ip, port, user, pwd)
        if hardware_id:
            update_config_section("vision", {"device_mac": hardware_id})
    except Exception:  # noqa: BLE001
        logger.debug("manual-ip MAC capture failed (non-fatal)", exc_info=True)
    return ApiResponse(data={"saved": True, "ip": ip})
```

- [ ] **Step 4: 加 ManualIpRequest schema**

在 `app/schema/api_schemas.py` 的 `AdvancedConfigRequest` 类之后(行 222 之后)加:

```python
class ManualIpRequest(BaseModel):
    """POST /discovery/manual-ip 请求体 — 手动填摄像头 IP 兜底。"""
    ip: str = ""

    @field_validator("ip")
    @classmethod
    def _ip_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if v:
            import ipaddress
            try:
                ipaddress.ip_address(v)
            except ValueError as e:
                raise ValueError(f"IP 格式错误: {e}") from e
        return v
```

- [ ] **Step 5: 注册路由到 main.py**

在 `app/main.py` 找到现有 `app.include_router(...)` 注册块(其他路由注册处),加:

```python
from .routes import discovery_routes
app.include_router(discovery_routes.router, prefix="/api")
```

(具体写法对齐 main.py 现有 include_router 的风格。)

- [ ] **Step 6: 运行验证通过**

Run: `pytest tests/test_discovery_routes.py::TestDiscoveryStatus -v`
Expected: PASS

- [ ] **Step 7: 写失败测试 — POST /discovery/find(手动触发)**

追加到 `tests/test_discovery_routes.py`:

```python
class TestTriggerFind:
    """POST /discovery/find: 手动触发发现。"""

    def test_find_disabled_returns_disabled(self, client, monkeypatch):
        from app.core import config as cfg
        cfg.CONFIG["vision"]["discovery_enabled"] = False
        with client:
            resp = client.post("/discovery/find")
        body = resp.json()
        assert resp.status_code == 200
        assert body["code"] == "disabled"

    def test_find_success(self, client, monkeypatch):
        from app.core import config as cfg
        from app.services import camera_discovery_service as cds
        cfg.CONFIG["vision"]["discovery_enabled"] = True
        monkeypatch.setattr(cds.discovery_service, "find_and_apply", AsyncMock(return_value="192.168.1.99"))
        monkeypatch.setattr(cds.discovery_service, "status", {"status": "found", "last_found_ip": "192.168.1.99", "last_error": "", "device_mac": ""})
        with client:
            resp = client.post("/discovery/find")
        body = resp.json()
        assert body["data"]["found"] is True
        assert body["data"]["ip"] == "192.168.1.99"
```

- [ ] **Step 8: 运行验证通过**

Run: `pytest tests/test_discovery_routes.py::TestTriggerFind -v`
Expected: PASS

- [ ] **Step 9: 写失败测试 — POST /discovery/manual-ip**

追加到 `tests/test_discovery_routes.py`:

```python
class TestManualIp:
    """POST /discovery/manual-ip: probe 验证 + 写 config。"""

    def test_probe_fail_rejects(self, client, monkeypatch):
        from app.services import config_probes
        from app.services.config_probes import ProbeResult
        monkeypatch.setattr(config_probes, "probe_ptz", AsyncMock(return_value=ProbeResult(ok=False, reason="unreachable", detail="连不上")))
        with client:
            resp = client.post("/discovery/manual-ip", json={"ip": "192.168.1.99"})
        body = resp.json()
        assert body["code"] == "probe_failed"
        assert body["data"]["saved"] is False

    def test_probe_pass_writes_config(self, client, monkeypatch):
        from app.services import config_probes
        from app.services.config_probes import ProbeResult
        from app.services import camera_discovery_service as cds
        monkeypatch.setattr(config_probes, "probe_ptz", AsyncMock(return_value=ProbeResult(ok=True)))
        monkeypatch.setattr(cds.discovery_service, "apply_found_ip", AsyncMock())
        monkeypatch.setattr(cds.discovery_service, "read_device_hardware_id", AsyncMock(return_value="aabbccddeeff"))
        with client:
            resp = client.post("/discovery/manual-ip", json={"ip": "192.168.1.99"})
        body = resp.json()
        assert body["data"]["saved"] is True
        assert body["data"]["ip"] == "192.168.1.99"

    def test_empty_ip_rejected(self, client):
        with client:
            resp = client.post("/discovery/manual-ip", json={"ip": ""})
        body = resp.json()
        assert body["code"] == "bad_format"
```

- [ ] **Step 10: 运行验证通过**

Run: `pytest tests/test_discovery_routes.py -v`
Expected: PASS(全部)

- [ ] **Step 11: 更新 config.example.json 补 discovery 字段**

在 `config.example.json` 的 `"vision"` 段里(`rtsp_password_env` 行之后)加示例字段:

```json
    "device_mac": "",
    "discovery_enabled": true,
    "discovery_timeout_seconds": 30,
    "discovery_subnet": "",
```

- [ ] **Step 12: commit**

```bash
git add app/routes/discovery_routes.py app/schema/api_schemas.py app/main.py config.example.json tests/test_discovery_routes.py
git commit -m "feat: 手动发现/手动填 IP 路由 + discovery 配置示例"
```

---

## Task 6: 端到端验证 + 边界场景测试

**Files:**
- Test: `tests/test_camera_discovery_service.py`(补:discovery_disabled、无凭证、find_and_apply 编排)

**目标:** 覆盖设计 §6 错误处理矩阵的剩余分支,确保向后兼容(discovery_enabled=false 时行为不变)。

- [ ] **Step 1: 写测试 — discovery_enabled=false 全链路不触发**

追加到 `tests/test_camera_discovery_service.py`:

```python
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
```

- [ ] **Step 2: 运行验证**

Run: `pytest tests/test_camera_discovery_service.py::TestDisabledAndEdgeCases -v`
Expected: PASS

- [ ] **Step 3: 运行全部相关测试套件(回归)**

Run: `pytest tests/test_camera_discovery_service.py tests/test_discovery_routes.py tests/test_ptz_service.py tests/test_ptz_config.py tests/test_advanced_routes_rtsp.py -v`
Expected: 全部 PASS(无回归)

- [ ] **Step 4: 运行整个测试套件确认无回归**

Run: `pytest -x -q`
Expected: 全部 PASS(若有不相关的预存失败,记录但不阻塞本任务 commit)

- [ ] **Step 5: commit**

```bash
git add tests/test_camera_discovery_service.py
git commit -m "test: ONVIF 发现边界场景(disabled/无凭证/编排)"
```

---

## 验收检查清单(对照 spec §8)

- [ ] config 里 `device_mac` 为空时启动,用现有 IP 能自动读到 MAC 并写入 config.json(Task 4 capture_mac_on_startup)
- [ ] 摄像头换 IP 后,RTSP 连续开流失败时自动触发发现,在 ~30s 内找回新 IP(Task 4 worker 触发)
- [ ] 发现成功后 PTZ 云台控制同时恢复(Task 3 apply_found_ip + notify_ip_changed)
- [ ] 发现超时未命中时,前端可查状态「找不到设备」并手动填新 IP(Task 5 routes)
- [ ] `discovery_enabled=false` 时行为与现状完全一致(Task 6 边界 + worker 守卫)
- [ ] 单测覆盖:子网推断、MAC 匹配、两段式扫描、超时、config 回写(Task 1-3)

## Self-Review 结论

**1. Spec coverage:**
- §1.1 约束 1(Docker/单播)→ Task 2 两段式单播扫描 ✅
- §1.1 约束 2(单设备/MAC 预留)→ Task1 normalize_mac + Task2 MAC 匹配 ✅
- §1.1 约束 3(MAC 身份证)→ Task1 read_device_hardware_id ✅
- §1.1 约束 4(被动+手动)→ Task4 worker 触发 + Task5 手动路由 ✅
- §1.1 约束 5(限子网)→ Task2 infer_subnet + _list_subnet_ips ✅
- §1.1 约束 6(RTSP+PTZ 同步)→ Task3 apply_found_ip 双写 ✅
- §1.1 约束 7(首次自动捕获)→ Task4 capture_mac_on_startup ✅
- §5.1 config 字段 → Task1 conftest + Task5 config.example ✅
- §6 错误处理(disabled/超时/手动/MAC缺失/无凭证)→ Task6 ✅
- 无遗漏。

**2. Placeholder scan:** 无 TBD/TODO;"读 main.py 行 X 确认变量名"是给实现者的定位指引(变量名因 main.py 现状需现场确认),非占位。已尽量给出具体行号。

**3. Type consistency:** 
- `find_camera` → `find_and_apply` → `apply_found_ip` 调用链类型一致(返回 str|None)。
- `read_device_hardware_id(ip, port, user, pwd)` 签名在 Task1 定义,Task2/4/5 调用一致。
- `notify_ip_changed(new_ip)` Task3 定义,Task3 包装函数 `ptz_service_notify_ip_changed` 一致。
- `discovery_service` 模块级单例已在 Task1 Step4 代码块末尾定义(Task4/5 import 依赖)。
