"""Tests for /api/advanced/config route — RTSP 摄像头源配置。

覆盖三个核心场景：
1. GET 返回 has_rtsp_password 标志（不回明文密码）
2. POST 带 rtsp_password → 走 write_secrets 写 .env，config.json 存变量名
3. POST 带 vision.rtsp_url → 落 config.json，并自动设 rtsp_password_env

直接调路由函数，避免导入 app.main（绕开 faiss 依赖）。
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock

from app.schema.api_schemas import AdvancedConfigRequest, VisionConfig
from app.services.config_probes import ProbeResult

# probe 通过的复用 mock：POST 带 rtsp_url 时会先 probe_rtsp 真连摄像头，
# 测试只验证"写配置"逻辑，probe 用 mock 放行，避免真实网络超时（容器内不可达）。
_PROBE_OK = AsyncMock(return_value=ProbeResult(ok=True))


class TestAdvancedConfigRtsp:
    """测试 RTSP 摄像头源配置接口。"""

    @pytest.mark.asyncio
    async def test_get_returns_has_rtsp_password_true_when_env_set(self):
        """GET：rtsp_password_env 已配且 .env 有对应变量 → has_rtsp_password=True。"""
        from app.routes import advanced_routes

        with patch.object(advanced_routes, "get_config") as mock_get, \
             patch("os.getenv", return_value="secret123"):
            mock_get.side_effect = lambda path, default=None: {
                "vision": {"rtsp_password_env": "RTSP_PASSWORD", "rtsp_url": "rtsp://x"},
                "web_search": {},
                "rag": {},
            }.get(path, default if default is not None else {})

            result = await advanced_routes.get_advanced_config()

        assert result.data["vision"]["has_rtsp_password"] is True
        # 确认不回传任何明文密码字段
        assert "rtsp_password" not in result.data["vision"]

    @pytest.mark.asyncio
    async def test_get_returns_has_rtsp_password_false_when_env_missing(self):
        """GET：rtsp_password_env 为空 → has_rtsp_password=False。"""
        from app.routes import advanced_routes

        with patch.object(advanced_routes, "get_config") as mock_get, \
             patch("os.getenv", return_value=""):
            mock_get.side_effect = lambda path, default=None: {
                "vision": {"rtsp_url": "", "rtsp_username": ""},
                "web_search": {},
                "rag": {},
            }.get(path, default if default is not None else {})

            result = await advanced_routes.get_advanced_config()

        assert result.data["vision"]["has_rtsp_password"] is False

    @pytest.mark.asyncio
    async def test_post_rtsp_password_writes_to_env_not_config(self):
        """POST 带 rtsp_password → 调 write_secrets，不进 vision 段。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://192.168.110.235:554/stream2", rtsp_username="admin"),
            rtsp_password="my_secret",
        )

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section") as mock_update, \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_OK):
            result = await advanced_routes.set_advanced_config(req)

        # write_secrets 被调用，参数是 {RTSP_PASSWORD: 明文}
        mock_write.assert_called_once_with({"RTSP_PASSWORD": "my_secret"})
        # update_config_section 被调三次：ptz.ip 同步、vision 段（含 rtsp_password_env）、补 env 变量名
        assert mock_update.call_count == 3
        # 第一次：ptz.ip 从 RTSP URL 自动同步
        first_call = mock_update.call_args_list[0]
        assert first_call.args[0] == "ptz"
        assert first_call.args[1] == {"ip": "192.168.110.235"}
        # 第二次：vision 段，含 rtsp_url 和 rtsp_password_env
        second_call = mock_update.call_args_list[1]
        assert second_call.args[0] == "vision"
        assert second_call.args[1]["rtsp_url"] == "rtsp://192.168.110.235:554/stream2"
        assert second_call.args[1]["rtsp_username"] == "admin"
        assert second_call.args[1]["rtsp_password_env"] == "RTSP_PASSWORD"
        # 确认 vision 段里没有明文密码
        assert "rtsp_password" not in second_call.args[1]
        # 第三次：补 rtsp_password_env（确保变量名落盘）
        third_call = mock_update.call_args_list[2]
        assert third_call.args[0] == "vision"
        assert third_call.args[1] == {"rtsp_password_env": "RTSP_PASSWORD"}
        assert result.data["saved"] is True

    @pytest.mark.asyncio
    async def test_post_empty_password_does_not_touch_env(self):
        """POST 密码留空 → 不调 write_secrets（保持原密码）。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://192.168.110.235:554/stream2"),
            rtsp_password="",  # 留空
        )

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section") as mock_update, \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_OK):
            await advanced_routes.set_advanced_config(req)

        mock_write.assert_not_called()
        # 调两次 update_config_section：vision 段 + ptz.ip 自动同步（rtsp_url 非空触发）
        assert mock_update.call_count == 2

    @pytest.mark.asyncio
    async def test_post_probe_fail_still_saves_password(self):
        """probe 失败时密码仍写 .env（防死循环：连不上→改不了密码→还是连不上）。

        probe 失败不应拦着密码落盘 —— 改密码往往正是因为连不上。URL/用户名
        不落盘（避免脏 config），但密码必须写进去，否则用户永远改不动密码。
        """
        from app.routes import advanced_routes

        _PROBE_FAIL = AsyncMock(return_value=ProbeResult(ok=False, reason="unauthorized", detail="凭证无效"))
        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://192.168.4.16:554/stream2", rtsp_username="admin"),
            rtsp_password="new_password",
        )

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section") as mock_update, \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_FAIL):
            result = await advanced_routes.set_advanced_config(req)

        # probe 失败 → 返回 probe_failed，saved=False
        assert result.code == "probe_failed"
        assert result.data["saved"] is False
        # 但密码仍写进了 .env
        mock_write.assert_called_once_with({"RTSP_PASSWORD": "new_password"})
        # password_saved 标志告诉前端密码已存
        assert result.data["password_saved"] is True

    @pytest.mark.asyncio
    async def test_post_probe_fail_no_password_writes_nothing(self):
        """probe 失败且没填密码 → 什么都不写（保持原样）。"""
        from app.routes import advanced_routes

        _PROBE_FAIL = AsyncMock(return_value=ProbeResult(ok=False, reason="unauthorized", detail="凭证无效"))
        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://192.168.4.16:554/stream2", rtsp_username="admin"),
            rtsp_password="",  # 没填密码
        )

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section") as mock_update, \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_FAIL):
            result = await advanced_routes.set_advanced_config(req)

        assert result.code == "probe_failed"
        assert result.data["saved"] is False
        mock_write.assert_not_called()
        assert result.data["password_saved"] is False

    @pytest.mark.asyncio
    async def test_post_usb_only_no_rtsp_url(self):
        """POST 留空 rtsp_url → 走 USB，不设 rtsp_password_env。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="", rtsp_username=""),
            rtsp_password="",
        )

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section") as mock_update:
            await advanced_routes.set_advanced_config(req)

        mock_write.assert_not_called()
        vision_call = mock_update.call_args_list[0]
        # rtsp_url 为空时不应自动设 rtsp_password_env
        assert "rtsp_password_env" not in vision_call.args[1]

    @pytest.mark.asyncio
    async def test_post_no_rtsp_password_env_leak_into_config_json(self):
        """安全测试：明文密码绝不进 config.json 的 vision 段。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://x", rtsp_username="admin"),
            rtsp_password="super_secret_123",
        )

        captured_vision = {}
        def fake_update(section, values):
            if section == "vision":
                captured_vision.update(values)
            return values

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section", side_effect=fake_update), \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_OK):
            await advanced_routes.set_advanced_config(req)

        # 所有写进 vision 段的 key 都不含明文密码
        for key, val in captured_vision.items():
            assert val != "super_secret_123", f"明文密码泄露到 config.json 的 {key} 字段"
        # 密码只进了 write_secrets
        mock_write.assert_called_once_with({"RTSP_PASSWORD": "super_secret_123"})


class TestRtspAutoSyncPtzIp:
    """保存 RTSP URL 时自动提取 IP → 同步到 ptz.ip。"""

    @pytest.mark.asyncio
    async def test_post_rtsp_url_auto_syncs_ptz_ip(self):
        """POST vision.rtsp_url → update_config_section 被调两次（vision + ptz），ptz 段含提取的 IP。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(
                rtsp_url="rtsp://admin:pass@192.168.110.235:554/stream2",
                rtsp_username="admin",
            ),
            rtsp_password="",
        )

        with patch.object(advanced_routes, "write_secrets") as mock_write, \
             patch.object(advanced_routes, "update_config_section") as mock_update, \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_OK):
            await advanced_routes.set_advanced_config(req)

        mock_write.assert_not_called()
        # 找到写 ptz 段的那次调用
        ptz_calls = [c for c in mock_update.call_args_list if c.args[0] == "ptz"]
        assert len(ptz_calls) == 1
        assert ptz_calls[0].args[1] == {"ip": "192.168.110.235"}

    @pytest.mark.asyncio
    async def test_post_rtsp_url_with_credentials_strips_auth(self):
        """RTSP URL 含 user:pass@ → 提取的 IP 不含凭据信息。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://cam_user:s3cr3t@10.0.0.8:554/"),
        )

        with patch.object(advanced_routes, "write_secrets"), \
             patch.object(advanced_routes, "update_config_section") as mock_update, \
             patch.object(advanced_routes, "probe_rtsp", _PROBE_OK):
            await advanced_routes.set_advanced_config(req)

        ptz_calls = [c for c in mock_update.call_args_list if c.args[0] == "ptz"]
        assert ptz_calls[0].args[1]["ip"] == "10.0.0.8"

    @pytest.mark.asyncio
    async def test_post_empty_rtsp_url_does_not_sync_ptz(self):
        """留空 rtsp_url（USB 模式）→ 不写 ptz.ip。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="", rtsp_username=""),
        )

        with patch.object(advanced_routes, "write_secrets"), \
             patch.object(advanced_routes, "update_config_section") as mock_update:
            await advanced_routes.set_advanced_config(req)

        ptz_calls = [c for c in mock_update.call_args_list if c.args[0] == "ptz"]
        assert len(ptz_calls) == 0

