"""语义图路由 — 触发构建 + 状态轮询 + 产物查询 + 节点搜索。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.config import update_config_section
from ..schema.api_schemas import SgConfigRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sg/status")
def get_sg_status(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """查询当前构建任务状态（前端轮询）。"""
    svc = container.sg_service
    if svc is None:
        return ApiResponse(code="sg_unavailable", message="语义图服务未就绪", data=None)
    return ApiResponse(data=svc.snapshot())


@router.post("/sg/build")
async def build_sg(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """触发语义图构建（异步，立即返回状态，前端轮询 /sg/status 看进度）。"""
    svc = container.sg_service
    if svc is None:
        return ApiResponse(code="sg_unavailable", message="语义图服务未就绪", data=None)
    result = await svc.build_async()
    if "error" in result:
        return ApiResponse(code="sg_busy", message=result["error"], data=result)
    return ApiResponse(data=result)


@router.post("/sg/cancel")
def cancel_sg(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """取消正在运行的构建任务。"""
    svc = container.sg_service
    if svc is None:
        return ApiResponse(code="sg_unavailable", message="语义图服务未就绪", data=None)
    svc.cancel()
    return ApiResponse(data=svc.snapshot())


@router.get("/sg/config")
def get_sg_config() -> ApiResponse[dict]:
    """返回当前 SG 构建参数（前端展示，不含密钥）。

    editable_keys 标明哪些参数允许用户修改（POST /sg/config 只接受这些）。
    """
    from ..sg.sg_config import SgConfig

    cfg = SgConfig.from_config()
    return ApiResponse(data={
        "ready": cfg.ready,
        "embed_model": cfg.embed_key.get("model", ""),
        "chat_model": cfg.chat_key.get("model", ""),
        "threshold": cfg.threshold,
        "pca_dim": cfg.pca_dim,
        "umap_n_components": cfg.umap_n_components,
        "umap_n_neighbors": cfg.umap_n_neighbors,
        "umap_min_dist": cfg.umap_min_dist,
        "umap_n_epochs": cfg.umap_n_epochs,
        "max_paragraph_chars": cfg.max_paragraph_chars,
        "max_workers": cfg.max_workers,
        "editable_keys": ["threshold", "max_workers", "umap_n_neighbors", "umap_min_dist"],
    })


@router.post("/sg/config")
async def set_sg_config(payload: SgConfigRequest) -> ApiResponse[dict]:
    """保存语义图可编辑参数到 config.json 的 sg 段。

    仅写入请求体中非 None 的字段（部分更新）。下次构建时 SgConfig.from_config()
    会读到最新值。
    """
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        return ApiResponse(data={"saved": False, "message": "无可更新字段"})
    update_config_section("sg", updates)
    logger.info("SG config updated: %s", list(updates.keys()))
    return ApiResponse(data={"saved": True, "updated": list(updates.keys())})


@router.get("/sg/latest")
def get_sg_latest() -> ApiResponse[dict]:
    """返回最近一次构建的 graph.json（供 3D 球视图加载）。

    无产物时返回 data=null + code=no_sg_output（200），避免 404 触发中间件二次拦截。
    """
    from ..services.sg_service import SemanticGraphService

    latest = SemanticGraphService.latest_graph()
    if not latest:
        return ApiResponse(code="no_sg_output", message="尚无语义图产物，请先构建", data=None)
    graph, task_dir = latest
    payload = {
        "graph": graph,
        "task_dir": task_dir.name,
        "node_count": len(graph.get("nodes", [])),
        "edge_count": len(graph.get("links", [])),
    }
    return ApiResponse(data=payload)


def _keyword_search(q: str, top_k: int, nodes: list[dict]) -> list[dict]:
    """关键词匹配退化路径：无向量索引或 embed 失败时使用。"""
    results = []
    query_words = set(q.lower().split())
    for n in nodes:
        name = n.get("name", "")
        category = n.get("category", "")
        text = (name + " " + category).lower()
        score = sum(1 for w in query_words if w in text) / max(len(query_words), 1)
        if score > 0:
            results.append({"id": n["id"], "title": name, "category": category, "score": round(score, 4)})
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def _to_vec_dict(raw, doc_ids: list[str]) -> dict:
    """把向量产物里的 raw_vectors 统一成 {doc_id: vec} 字典。

    新格式(npz)是矩阵(行顺序与 doc_ids 对齐)；老格式(pickle)是 dict[str, vec]。
    """
    import numpy as np
    if isinstance(raw, np.ndarray) and raw.ndim == 2:
        return {did: raw[i] for i, did in enumerate(doc_ids)}
    # 老格式：被 np.load 包成 0-d array 的 dict，用 .item() 还原
    if isinstance(raw, np.ndarray):
        raw = raw.item()
    return dict(raw)


@router.get("/sg/search")
async def sg_search(q: str = "", top_k: int = 10, container: AppContainer = Depends(get_container)):
    """向量检索：用 embed_client 给 query 取向量，在最近构建的 faiss 索引里搜近邻。

    无产物或无索引时退化为关键词匹配。
    """
    if not q:
        return {"results": []}
    from ..services.sg_service import SemanticGraphService

    latest = SemanticGraphService.latest_graph()
    if not latest:
        return {"results": []}
    graph, task_dir = latest
    vectors_path = task_dir / "vectors.pkl"
    faiss_path = task_dir / "models" / "faiss.index"
    nodes = graph.get("nodes", [])
    if not vectors_path.exists() or not faiss_path.exists():
        return {"results": _keyword_search(q, top_k, nodes)}

    import numpy as np
    import faiss

    # 读取向量产物：新格式为 npz（allow_pickle=False 安全），老格式为纯 pickle
    # （本地自产文件，回退 allow_pickle=True 仅作兼容，无不可信外部输入）
    try:
        vd = np.load(str(vectors_path), allow_pickle=False)
    except ValueError:
        vd = np.load(str(vectors_path), allow_pickle=True)
    doc_ids = list(vd["doc_ids"])

    # query 向量化：优先复用产物中已存在的向量，否则调 embed_client
    # 新格式 raw_vectors 是矩阵（行顺序与 doc_ids 对齐）；老格式是 dict[str, vec]
    raw_vecs = _to_vec_dict(vd["raw_vectors"], doc_ids)
    if q in raw_vecs:
        q_vec = raw_vecs[q]
    else:
        embed_client = container.embed_client
        try:
            result = await embed_client.post_embedding({
                "model": embed_client.model,
                "prompt": q,
            })
            q_vec = np.array(result["embedding"], dtype=np.float32)
        except Exception:
            logger.warning("search: embed 失败，退化为关键词匹配")
            return {"results": _keyword_search(q, top_k, nodes)}

    q_vec = q_vec.reshape(1, -1).astype(np.float32)
    faiss.normalize_L2(q_vec)
    idx = faiss.read_index(str(faiss_path))
    scores, indices = idx.search(q_vec, min(top_k * 2, idx.ntotal))
    id_to_node = {n["id"]: n for n in nodes}
    results = []
    for score, i in zip(scores[0], indices[0]):
        did = doc_ids[i]
        node = id_to_node.get(did)
        if not node:
            continue
        results.append({"id": node["id"], "title": node.get("name", node["id"]), "category": node.get("category", ""), "score": round(float(score), 4)})
    return {"results": results[:top_k]}
