"""Tests for /api/advanced/config — 视觉参数保存(RTSP 源配置已移除)。

多摄像头体系下 RTSP 源(url/用户名/密码)归 cameras 表,由「摄像头设置」页
per-camera 管理(试连走 /api/cameras/{id}/test-stream)。本路由只存视觉
处理参数,不 probe、不写 ptz.ip、不碰 .env。

直接调路由函数，避免导入 app.main（绕开 faiss 依赖）。
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from app.schema.api_schemas import AdvancedConfigRequest, VisionConfig


class TestAdvancedConfigVisionParamsOnly:
    """视觉参数保存:纯参数落盘,任何遗留 rtsp 字段不触发 probe/ptz 同步。"""

    @pytest.mark.asyncio
    async def test_post_vision_params_saved_without_probe(self):
        """POST 视觉参数 → 只写 vision 段,不 probe、不同步 ptz。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(motion_threshold=20, min_infer_interval_seconds=5.0),
        )

        with patch.object(advanced_routes, "update_config_section") as mock_update:
            result = await advanced_routes.set_advanced_config(req)

        assert result.data["saved"] is True
        assert mock_update.call_count == 1
        assert mock_update.call_args.args[0] == "vision"
        values = mock_update.call_args.args[1]
        assert values["motion_threshold"] == 20
        assert values["min_infer_interval_seconds"] == 5.0
        # 不写 ptz 段(那是单摄时代从 RTSP URL 提取 IP 的逻辑)
        ptz_calls = [c for c in mock_update.call_args_list if c.args[0] == "ptz"]
        assert ptz_calls == []

    @pytest.mark.asyncio
    async def test_post_with_legacy_rtsp_password_ignored(self):
        """旧前端仍传 rtsp_password → 忽略,不写 .env(凭证在摄像头设置改)。

        本路由已不导入 write_secrets（模块级不存在该名字即不可能写 .env），
        这里只验证保存成功且 rtsp_password 不会渗进任何配置段。
        """
        from app.routes import advanced_routes

        assert not hasattr(advanced_routes, "write_secrets")

        req = AdvancedConfigRequest(
            vision=VisionConfig(motion_threshold=15),
            rtsp_password="should_be_ignored",
        )

        with patch.object(advanced_routes, "update_config_section") as mock_update:
            result = await advanced_routes.set_advanced_config(req)

        assert result.data["saved"] is True
        mock_update.assert_called_once()
        assert "should_be_ignored" not in str(mock_update.call_args)

    @pytest.mark.asyncio
    async def test_post_legacy_rtsp_url_never_blocks_save(self):
        """vision 段捎带了旧 rtsp_url 字段(前端缓存) → 原样落盘字段本身,
        但不做任何连接验证——旧行为里这会 probe 一个永不更新的 IP 并卡死保存。"""
        from app.routes import advanced_routes

        req = AdvancedConfigRequest(
            vision=VisionConfig(rtsp_url="rtsp://stale-ip:554/stream2", rtsp_username="admin"),
        )

        with patch.object(advanced_routes, "probe_exa") as mock_probe_exa, \
             patch.object(advanced_routes, "update_config_section") as mock_update:
            result = await advanced_routes.set_advanced_config(req)

        assert result.data["saved"] is True
        assert result.code == "ok"
        mock_probe_exa.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_returns_has_rtsp_password_flag(self):
        """GET:密码不回传明文,只回「是否已配置」标志(遗留字段,保持兼容)。"""
        from app.routes import advanced_routes

        with patch.object(advanced_routes, "get_config") as mock_get, \
             patch("os.getenv", return_value="secret123"):
            mock_get.side_effect = lambda path, default=None: {
                "vision": {"rtsp_password_env": "RTSP_PASSWORD"},
                "web_search": {},
                "rag": {},
            }.get(path, default if default is not None else {})

            result = await advanced_routes.get_advanced_config()

        assert result.data["vision"]["has_rtsp_password"] is True
        assert "rtsp_password" not in result.data["vision"]
