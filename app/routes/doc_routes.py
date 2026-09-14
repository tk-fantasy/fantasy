"""文档/RAG 路由 — 文档内容查询与 RAG 索引管理（对话走 /ws/doc/chat WebSocket）。

语义图相关的构建/查询/搜索已统一迁移至 sg_routes。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/doc/content")
def doc_content(doc_id: str = ""):
    """按 doc_id 精确返回 docs/ 下 markdown 内容（知识图谱节点详情用）。

    必须挂 /api 前缀：api_token_guard 中间件只保护 /api/* 路径，
    挂在外面会变成免认证的文档读取口。
    """
    if not doc_id:
        raise AppException("doc_id is required", code="doc_id_required", http_status=400)
    # docs 目录基于项目根（app/ 的父目录），不依赖 app.main 全局变量
    docs_root = Path(os.environ.get("DOCS_ROOT") or str(Path(__file__).resolve().parent.parent.parent / "docs"))
    for md in docs_root.rglob("*.md"):
        if md.stem == doc_id:
            return {"content": md.read_text(encoding="utf-8")}
    raise AppException("document not found", code="document_not_found", http_status=404)


@router.post("/api/doc/rebuild")
async def rebuild_doc_index(container: AppContainer = Depends(get_container)) -> dict:
    """触发 RAG 索引后台重建（换 embed 模型或更新 docs 后调用）。"""
    rag_service = container.rag_service
    if rag_service is None:
        raise AppException("RAG service not available", code="rag_unavailable", http_status=503)
    if not container.embed_client.enabled:
        raise AppException("Embed 模型未配置，请先在设置页配置 LLM Key (type=embed)",
                           code="embed_not_configured", http_status=400)
    if rag_service._rebuilding:
        return {"status": "already_running", "message": "重建正在进行中"}

    from ..main import _stream_executor
    rag_service._rebuilding = True
    _stream_executor.submit(rag_service.safe_build)
    return {"status": "started", "message": "索引重建已开始"}


@router.get("/api/doc/rebuild/status")
def doc_rebuild_status(container: AppContainer = Depends(get_container)) -> dict:
    """查询 RAG 索引重建状态（含进度：total/done/errors/message）。"""
    rag_service = container.rag_service
    if rag_service is None:
        return {"rebuilding": False, "total": 0, "done": 0, "errors": 0,
                "message": "", "model": "", "chunk_count": 0}
    return rag_service.rebuild_status
