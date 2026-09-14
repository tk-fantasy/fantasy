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


