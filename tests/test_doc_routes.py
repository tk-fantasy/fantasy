"""Tests for doc_routes.py - RAG 文档助手与文档内容查询。

语义图搜索相关测试已随路由迁移至 test_sg_routes.py。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDocContentRoute:
    """测试 /doc/content 路由。"""

    def test_doc_not_found(self):
        """文档不存在时抛出 404。"""
        from app.routes.doc_routes import doc_content
        from app.core.exceptions import AppException

        with patch("app.routes.doc_routes.Path.rglob", return_value=[]):
            with pytest.raises(AppException) as exc_info:
                doc_content(doc_id="nonexistent")
            assert exc_info.value.http_status == 404

    def test_doc_id_required(self):
        """doc_id 为空时抛出 400。"""
        from app.routes.doc_routes import doc_content
        from app.core.exceptions import AppException

        with pytest.raises(AppException) as exc_info:
            doc_content(doc_id="")
        assert exc_info.value.http_status == 400


class TestDocChatRoute:
    """测试 /api/doc/chat 路由。"""

    @pytest.mark.asyncio
    async def test_rag_not_ready(self):
        """RAG 索引未就绪时抛出 503。"""
        from app.routes.doc_routes import doc_chat
        from app.core.exceptions import AppException

        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={"message": "test"})
        mock_container = MagicMock()
        mock_container.rag_service = None  # RAG 服务未初始化

        with pytest.raises(AppException) as exc_info:
            await doc_chat(mock_request, container=mock_container)
        assert exc_info.value.http_status == 503

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        """无效 JSON 时抛出 400。"""
        from app.routes.doc_routes import doc_chat
        from app.core.exceptions import AppException

        mock_request = MagicMock()
        mock_request.json = AsyncMock(side_effect=Exception("invalid json"))
        mock_container = MagicMock()
        mock_container.rag_service.is_ready = True  # RAG 就绪，走到 json 解析

        with pytest.raises(AppException) as exc_info:
            await doc_chat(mock_request, container=mock_container)
        assert exc_info.value.http_status == 400

    @pytest.mark.asyncio
    async def test_empty_message(self):
        """空消息时抛出 400。"""
        from app.routes.doc_routes import doc_chat
        from app.core.exceptions import AppException

        mock_request = MagicMock()
        mock_request.json = AsyncMock(return_value={"message": ""})
        mock_container = MagicMock()
        mock_container.rag_service.is_ready = True  # RAG 就绪，走到 message 检查

        with pytest.raises(AppException) as exc_info:
            await doc_chat(mock_request, container=mock_container)
        assert exc_info.value.http_status == 400
