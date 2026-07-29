"""Tests for app/routes/discovery_routes.py — 手动发现 + 手动填 IP。

discovery_service 单例 mock,验证路由的编排逻辑(status 查询、手动触发、
手动 IP 验证 + 写 config)。

测试方式说明:本测试环境缺 numpy/faiss(预存环境问题),`from app.main
import app` 会因 main.py 顶部 `import faiss`/`import numpy` 失败,无法走
TestClient/HTTP 层。改为直接 import discovery_routes 模块、直接 await 路由
async 函数,断言返回的 ApiResponse 结构(code/message/data)。这样绕开 main.py
的 numpy 依赖,同时仍真正验证路由的编排逻辑(凭证校验、config 回写、
discovery_service 单例调用编排)。与现有 test_advanced_routes_rtsp.py 同款。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDiscoveryStatus:
    """GET /discovery/status: 返回发现服务状态。"""

    @pytest.mark.asyncio
    async def test_status_returns_fields(self):
        from app.routes import discovery_routes

        fake_status = {
            "status": "idle",
            "last_found_ip": "",
            "last_error": "",
            "device_mac": "aabbccddeeff",
        }
        # discovery_service 是模块级单例,直接 patch 它的 status property
        with patch.object(type(discovery_routes.discovery_service), "status",
                          new_callable=lambda: property(lambda self: fake_status)):
            result = await discovery_routes.get_discovery_status()

        assert result.code == "ok"
        assert result.data["status"] == "idle"
        assert result.data["device_mac"] == "aabbccddeeff"


class TestTriggerFind:
    """POST /discovery/find: 手动触发发现。"""

    @pytest.mark.asyncio
    async def test_find_disabled_returns_disabled(self):
        """discovery_enabled=False → code=disabled,不调 find_and_apply。"""
        from app.core import config as cfg
        from app.routes import discovery_routes

        cfg.CONFIG["vision"]["discovery_enabled"] = False
        find_mock = AsyncMock()
        with patch.object(discovery_routes.discovery_service, "find_and_apply", find_mock):
            result = await discovery_routes.trigger_discovery()

        assert result.code == "disabled"
        assert result.data["found"] is False
        find_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_find_success(self):
        """discovery_enabled=True 且 find_and_apply 返回 IP → found=True, ip 回填。"""
        from app.core import config as cfg
        from app.routes import discovery_routes

        cfg.CONFIG["vision"]["discovery_enabled"] = True
        fake_status = {
            "status": "found",
            "last_found_ip": "192.168.1.99",
            "last_error": "",
            "device_mac": "",
        }
        with patch.object(discovery_routes.discovery_service, "find_and_apply",
                          AsyncMock(return_value="192.168.1.99")), \
             patch.object(type(discovery_routes.discovery_service), "status",
                          new_callable=lambda: property(lambda self: fake_status)):
            result = await discovery_routes.trigger_discovery()

        assert result.code == "ok"
        assert result.data["found"] is True
        assert result.data["ip"] == "192.168.1.99"

    @pytest.mark.asyncio
    async def test_find_not_found(self):
        """discovery_enabled=True 但 find_and_apply 返回 None → code=not_found。"""
        from app.core import config as cfg
        from app.routes import discovery_routes

        cfg.CONFIG["vision"]["discovery_enabled"] = True
        fake_status = {
            "status": "not_found",
            "last_found_ip": "",
            "last_error": "未找到设备",
            "device_mac": "",
        }
        with patch.object(discovery_routes.discovery_service, "find_and_apply",
                          AsyncMock(return_value=None)), \
             patch.object(type(discovery_routes.discovery_service), "status",
                          new_callable=lambda: property(lambda self: fake_status)):
            result = await discovery_routes.trigger_discovery()

        assert result.code == "not_found"
        assert result.data["found"] is False


class TestManualIp:
    """POST /discovery/manual-ip: probe 验证 + 写 config。"""

    @pytest.mark.asyncio
    async def test_probe_fail_rejects(self):
        """probe 失败 → code=probe_failed, 不写 config(saved=False)。"""
        from app.routes import discovery_routes
        from app.services.config_probes import ProbeResult

        apply_mock = AsyncMock()
        with patch.object(discovery_routes, "probe_ptz",
                          AsyncMock(return_value=ProbeResult(ok=False, reason="unreachable", detail="连不上"))), \
             patch.object(discovery_routes.discovery_service, "apply_found_ip", apply_mock):
            result = await discovery_routes.set_manual_ip(
                discovery_routes.ManualIpRequest(ip="192.168.1.99")
            )

        assert result.code == "probe_failed"
        assert result.data["saved"] is False
        apply_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_pass_writes_config(self):
        """probe 通过 → apply_found_ip 写 config + 捕获 MAC,saved=True。"""
        from app.routes import discovery_routes
        from app.services.config_probes import ProbeResult

        apply_mock = AsyncMock()
        read_hw_mock = AsyncMock(return_value="aabbccddeeff")
        with patch.object(discovery_routes, "probe_ptz",
                          AsyncMock(return_value=ProbeResult(ok=True))), \
             patch.object(discovery_routes.discovery_service, "apply_found_ip", apply_mock), \
             patch.object(discovery_routes.discovery_service, "read_device_hardware_id", read_hw_mock), \
             patch.object(discovery_routes, "update_config_section") as uc:
            result = await discovery_routes.set_manual_ip(
                discovery_routes.ManualIpRequest(ip="192.168.1.99")
            )

        assert result.code == "ok"
        assert result.data["saved"] is True
        assert result.data["ip"] == "192.168.1.99"
        # apply_found_ip 被调用写 config
        apply_mock.assert_called_once_with("192.168.1.99")
        # MAC 被捕获并写回 vision.device_mac
        read_hw_mock.assert_called_once()
        uc.assert_called_once_with("vision", {"device_mac": "aabbccddeeff"})

    @pytest.mark.asyncio
    async def test_empty_ip_rejected(self):
        """空 IP → code=bad_format(ManualIpRequest 允许空串,路由层挡)。"""
        from app.routes import discovery_routes

        result = await discovery_routes.set_manual_ip(
            discovery_routes.ManualIpRequest(ip="")
        )

        assert result.code == "bad_format"
        assert result.data["saved"] is False
