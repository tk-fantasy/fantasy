"""Tests for PTZ configuration — IP 提取 + GET/POST /ptz/config。

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
from unittest.mock import patch

from app.schema.api_schemas import PtzConfigRequest


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


# --------------- GET /ptz/config ---------------

class TestGetPtzConfig:
    @pytest.mark.asyncio
    async def test_returns_has_password_true(self):
        from app.routes import ptz_routes

        with patch.object(ptz_routes, "get_config") as mock_get, \
             patch("os.getenv", return_value="secret"):
            mock_get.side_effect = lambda path, default=None: {
                "ptz": {"password_env": "PTZ_PASSWORD", "ip": "10.0.0.1", "enabled": True},
            }.get(path, default if default is not None else {})

            result = await ptz_routes.ptz_config_get()

        assert result.data["has_password"] is True
        assert result.data["ip"] == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_returns_has_password_false_when_env_missing(self):
        from app.routes import ptz_routes

        with patch.object(ptz_routes, "get_config") as mock_get, \
             patch("os.getenv", return_value=""):
            mock_get.side_effect = lambda path, default=None: {
                "ptz": {"password_env": "", "ip": ""},
            }.get(path, default if default is not None else {})

            result = await ptz_routes.ptz_config_get()

        assert result.data["has_password"] is False


# --------------- POST /ptz/config ---------------

class TestPostPtzConfig:
    @pytest.mark.asyncio
    async def test_password_writes_to_env_not_config(self):
        """POST 带 password → write_secrets 被调，config.json 不含明文。"""
        from app.routes import ptz_routes

        req = PtzConfigRequest(
            enabled=True, ip="192.168.1.100", port=2020,
            username="admin", password="onvif_secret",
            speed=0.5, step_ms=300,
        )

        with patch.object(ptz_routes, "write_secrets") as mock_write, \
             patch.object(ptz_routes, "update_config_section") as mock_update:
            result = await ptz_routes.ptz_config_set(req)

        mock_write.assert_called_once_with({"PTZ_PASSWORD": "onvif_secret"})
        # update_config_section 被调两次：一次写 ptz 段，一次补 password_env
        assert mock_update.call_count == 2
        first_call = mock_update.call_args_list[0]
        assert first_call.args[0] == "ptz"
        assert first_call.args[1]["ip"] == "192.168.1.100"
        assert first_call.args[1]["username"] == "admin"
        # 明文密码绝不进 config.json
        assert "password" not in first_call.args[1]
        second_call = mock_update.call_args_list[1]
        assert second_call.args[0] == "ptz"
        assert second_call.args[1] == {"password_env": "PTZ_PASSWORD"}
        assert result.data["saved"] is True

    @pytest.mark.asyncio
    async def test_empty_password_does_not_touch_env(self):
        """密码留空 → 不调 write_secrets。"""
        from app.routes import ptz_routes

        req = PtzConfigRequest(
            enabled=False, ip="10.0.0.1", port=80,
            username="user", password="",
        )

        with patch.object(ptz_routes, "write_secrets") as mock_write, \
             patch.object(ptz_routes, "update_config_section") as mock_update:
            await ptz_routes.ptz_config_set(req)

        mock_write.assert_not_called()
        # 只调一次 update_config_section（ptz 段），不会补 password_env
        assert mock_update.call_count == 1

    @pytest.mark.asyncio
    async def test_all_fields_saved(self):
        """POST → update_config_section 收到全部配置字段。"""
        from app.routes import ptz_routes

        req = PtzConfigRequest(
            enabled=True, ip="172.16.0.5", port=8080,
            username="onvif_user", password="",
            speed=0.8, step_ms=500,
        )

        with patch.object(ptz_routes, "write_secrets") as mock_write, \
             patch.object(ptz_routes, "update_config_section") as mock_update:
            await ptz_routes.ptz_config_set(req)

        mock_write.assert_not_called()
        config_data = mock_update.call_args_list[0].args[1]
        assert config_data == {
            "enabled": True,
            "ip": "172.16.0.5",
            "port": 8080,
            "username": "onvif_user",
            "speed": 0.8,
            "step_ms": 500,
        }

    @pytest.mark.asyncio
    async def test_no_plaintext_password_leak(self):
        """安全测试：明文密码绝不进 config.json 的 ptz 段。"""
        from app.routes import ptz_routes

        req = PtzConfigRequest(
            enabled=True, ip="1.2.3.4", port=80,
            username="admin", password="super_secret_456",
        )

        captured_ptz = {}

        def fake_update(section, values):
            if section == "ptz":
                captured_ptz.update(values)
            return values

        with patch.object(ptz_routes, "write_secrets") as mock_write, \
             patch.object(ptz_routes, "update_config_section", side_effect=fake_update):
            await ptz_routes.ptz_config_set(req)

        for key, val in captured_ptz.items():
            assert val != "super_secret_456", f"明文密码泄露到 config.json 的 {key} 字段"
        mock_write.assert_called_once_with({"PTZ_PASSWORD": "super_secret_456"})
