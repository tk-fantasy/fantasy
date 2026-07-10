"""Tests for home routes."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestGetHomeInfo:
    @pytest.mark.asyncio
    async def test_get_home_info_empty(self):
        """测试获取家庭信息（空配置）。"""
        from app.routes.home_routes import get_home_info

        mock_user = {"user_id": "test-user", "username": "test"}
        mock_db = AsyncMock()
        mock_db.user_settings_all = AsyncMock(return_value={})

        with patch("app.routes.home_routes.Database") as MockDB:
            MockDB.get.return_value = mock_db
            result = await get_home_info(current_user=mock_user)
            assert result.code == "ok"
            assert result.data["home_name"] == ""
            assert result.data["owner_name"] == ""
            assert result.data["province"] == ""

    @pytest.mark.asyncio
    async def test_get_home_info_with_data(self):
        """测试获取家庭信息（有数据）。"""
        from app.routes.home_routes import get_home_info
        import json

        mock_user = {"user_id": "test-user", "username": "test"}
        home_data = {
            "home_name": "童柯诚的家",
            "owner_name": "童柯诚",
            "province": "上海市",
            "city": "上海市",
            "district": "宝山区",
        }
        mock_db = AsyncMock()
        mock_db.user_settings_all = AsyncMock(return_value={"home_info": json.dumps(home_data)})

        with patch("app.routes.home_routes.Database") as MockDB:
            MockDB.get.return_value = mock_db
            result = await get_home_info(current_user=mock_user)
            assert result.data["home_name"] == "童柯诚的家"
            assert result.data["owner_name"] == "童柯诚"
            assert result.data["province"] == "上海市"
            assert result.data["district"] == "宝山区"


class TestSetHomeInfo:
    @pytest.mark.asyncio
    async def test_set_home_info(self):
        """测试更新家庭信息。"""
        from app.routes.home_routes import set_home_info
        from app.schema.api_schemas import HomeInfoRequest
        import json

        mock_user = {"user_id": "test-user", "username": "test"}
        mock_db = AsyncMock()
        mock_db.user_settings_all = AsyncMock(return_value={})
        mock_db.user_setting_set = AsyncMock()

        with patch("app.routes.home_routes.Database") as MockDB:
            MockDB.get.return_value = mock_db
            result = await set_home_info(
                payload=HomeInfoRequest(
                    home_name="我的家",
                    owner_name="Test",
                    province="北京市",
                    city="北京市",
                    district="朝阳区",
                ),
                current_user=mock_user,
            )
            assert result.code == "ok"
            assert result.data["home_name"] == "我的家"
            assert result.data["province"] == "北京市"
            # 验证调用了 user_setting_set
            mock_db.user_setting_set.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_home_info_partial(self):
        """测试部分更新家庭信息。"""
        from app.routes.home_routes import set_home_info
        from app.schema.api_schemas import HomeInfoRequest
        import json

        mock_user = {"user_id": "test-user", "username": "test"}
        existing_data = {
            "home_name": "旧名字",
            "owner_name": "Old",
            "province": "",
            "city": "",
            "district": "",
        }
        mock_db = AsyncMock()
        mock_db.user_settings_all = AsyncMock(return_value={"home_info": json.dumps(existing_data)})
        mock_db.user_setting_set = AsyncMock()

        with patch("app.routes.home_routes.Database") as MockDB:
            MockDB.get.return_value = mock_db
            result = await set_home_info(
                payload=HomeInfoRequest(home_name="新名字"),
                current_user=mock_user,
            )
            assert result.data["home_name"] == "新名字"
            assert result.data["owner_name"] == "Old"  # 保留原有值
