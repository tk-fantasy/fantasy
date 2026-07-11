"""文档/RAG 路由 — RAG 文档助手与文档内容查询。

语义图相关的构建/查询/搜索已统一迁移至 sg_routes。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..container import AppContainer, get_container
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/doc/chat")
async def doc_chat(request: Request, container: AppContainer = Depends(get_container)) -> StreamingResponse:
    import asyncio
    from ..services.prompt_service import RAG_SYSTEM_PROMPT_TEMPLATE

    rag_service = container.rag_service
    if rag_service is None or not rag_service.is_ready:
        raise AppException("RAG index not ready", code="rag_not_ready", http_status=503)

    try:
        body = await request.json()
    except Exception:
        raise AppException("invalid JSON body", code="invalid_json", http_status=400)
    message = body.get("message", "")
    if not message:
        raise AppException("message is required", code="message_required", http_status=400)

    # 1. RAG 搜索（��败时降级为无上下文，不影响 LLM 回答）
    try:
        context = await rag_service.search(message)
    except Exception as e:
        logger.warning("doc_chat: RAG 检索失败，降级为无上下文: %s", e)
        context = ""
    system = RAG_SYSTEM_PROMPT_TEMPLATE.format(context=context)

    # 2. LLM 流式调用
    client, chat_model = rag_service.build_llm_client()
    loop = asyncio.get_event_loop()

    def _run_stream():
        return client.chat.completions.create(
            model=chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": message}
            ],
            stream=True
        )

    async def generate():
        try:
            stream = await loop.run_in_executor(None, _run_stream)
            for chunk in stream:
                if chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'token': f'[错误] {str(e)}'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/doc/content")
def doc_content(doc_id: str = ""):
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
    """查询 RAG 索引重建状态。"""
    rag_service = container.rag_service
    if rag_service is None:
        return {"rebuilding": False, "model": "", "chunk_count": 0}
    return {
        "rebuilding": rag_service._rebuilding,
        "model": rag_service._embed_model,
        "chunk_count": rag_service.chunk_count,
    }
