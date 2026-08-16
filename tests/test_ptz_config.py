"""Tests for PTZ 工具函数 extract_host_from_url（旧全局 /ptz/config 路由已随 PTZ 双体系收敛删除）。

覆盖：
1. extract_host_from_url：各种 RTSP URL 格式 → 正确提取 IP
2. GET /ptz/config：返回 has_password 标志（不回明文）
3. POST /ptz/config：密码写 .env，config.json 只存变量名
4. POST 密码留空 → 不调 write_secrets
5. POST 所有字段正确落 config.json

直接调路由函数，避免导入 app.main（绕开 faiss 依赖）。
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock

from app.schema.api_schemas import PtzConfigRequest
from app.services.config_probes import ProbeResult

# probe 通过的复用 mock：POST /ptz/config 在 enabled+ip 时会先 probe_ptz 真连摄像头，
# 测试只验证"写配置"逻辑，probe 用 mock 放行，避免真实网络超时（容器内不可达）。
_PROBE_OK = AsyncMock(return_value=ProbeResult(ok=True))


# --------------- extract_host_from_url ---------------

class TestExtractHostFromUrl:
    def test_with_credentials_and_port(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url("rtsp://admin:pass@192.168.1.100:554/stream") == "192.168.1.100"

    def test_with_port_no_credentials(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url("rtsp://192.168.1.100:554/stream") == "192.168.1.100"

    def test_without_port(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url("rtsp://192.168.1.100/stream") == "192.168.1.100"

    def test_empty_string(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url("") == ""

    def test_whitespace_only(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url("   ") == ""

    def test_none_like(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url(None) == ""

    def test_hostname_not_ip(self):
        from app.services.ptz_service import extract_host_from_url
        assert extract_host_from_url("rtsp://cam.example.com:8554/ch1") == "cam.example.com"
