"""Tests for PTZ 工具函数 extract_host_from_url（旧全局 /ptz/config 路由已随 PTZ 双体系收敛删除）。"""
from __future__ import annotations


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
