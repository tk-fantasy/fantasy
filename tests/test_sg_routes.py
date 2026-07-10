"""Tests for app/routes/sg_routes.py — 语义图路由。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_container(sg_service=None):
    """构造一个带 sg_service 的假 container。"""
    c = MagicMock()
    c.sg_service = sg_service
    return c


class TestGetSgStatus:
    def test_service_unavailable(self):
        """sg_service 为 None 时返回 sg_unavailable 而非 ok。"""
        from app.routes.sg_routes import get_sg_status

        container = _mock_container(sg_service=None)
        resp = get_sg_status(container=container)
        assert resp.code == "sg_unavailable"
        assert "未就绪" in resp.message
        assert resp.data is None

    def test_returns_snapshot(self):
        from app.routes.sg_routes import get_sg_status

        svc = MagicMock()
        svc.snapshot.return_value = {"status": "running", "progress": 50, "message": "向量化"}
        container = _mock_container(sg_service=svc)
        resp = get_sg_status(container=container)
        assert resp.code == "ok"
        assert resp.data["progress"] == 50


class TestCancelSg:
    def test_service_unavailable(self):
        from app.routes.sg_routes import cancel_sg

        container = _mock_container(sg_service=None)
        resp = cancel_sg(container=container)
        assert resp.code == "sg_unavailable"

    def test_cancel_calls_service(self):
        from app.routes.sg_routes import cancel_sg

        svc = MagicMock()
        svc.snapshot.return_value = {"status": "idle", "progress": 0, "message": "正在取消..."}
        container = _mock_container(sg_service=svc)
        resp = cancel_sg(container=container)
        svc.cancel.assert_called_once()
        assert resp.code == "ok"


class TestGetSgConfig:
    def test_config_ready(self, monkeypatch):
        """已配置 embed+chat key 时 ready=True，模型名正确返回。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [
                {"type": "embed", "model": "BAAI/bge-m3"},
                {"type": "chat", "model": "glm-4-flash"},
            ],
            "sg": {"threshold": 0.85, "pca_dim": 64},
        })

        from app.routes.sg_routes import get_sg_config
        resp = get_sg_config()
        assert resp.code == "ok"
        assert resp.data["ready"] is True
        assert resp.data["embed_model"] == "BAAI/bge-m3"
        assert resp.data["chat_model"] == "glm-4-flash"
        assert resp.data["threshold"] == 0.85

    def test_config_not_ready(self, monkeypatch):
        """未配置 key 时 ready=False，模型名为空。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {"llm_keys": [], "sg": {}})

        from app.routes.sg_routes import get_sg_config
        resp = get_sg_config()
        assert resp.data["ready"] is False
        assert resp.data["embed_model"] == ""

    def test_config_no_api_key_leaked(self, monkeypatch):
        """返回数据不应包含 api_key（仅展示模型名和参数）。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [
                {"type": "embed", "model": "bge-m3", "api_key": "sk-secret"},
                {"type": "chat", "model": "glm-4-flash", "api_key": "sk-chat-secret"},
            ],
            "sg": {},
        })

        from app.routes.sg_routes import get_sg_config
        resp = get_sg_config()
        serialized = json.dumps(resp.model_dump(), ensure_ascii=False)
        assert "sk-secret" not in serialized
        assert "sk-chat-secret" not in serialized

    def test_config_returns_full_params_and_editable_keys(self, monkeypatch):
        """GET 返回全部参数 + editable_keys 标明哪些可改。"""
        import app.core.config as cfg
        monkeypatch.setattr(cfg, "CONFIG", {
            "llm_keys": [
                {"type": "embed", "model": "bge-m3"},
                {"type": "chat", "model": "glm-4-flash"},
            ],
            "sg": {"umap_n_neighbors": 25, "umap_min_dist": 0.2, "max_workers": 4},
        })

        from app.routes.sg_routes import get_sg_config
        resp = get_sg_config()
        assert resp.data["umap_n_neighbors"] == 25
        assert resp.data["umap_min_dist"] == 0.2
        assert resp.data["max_workers"] == 4
        assert resp.data["umap_n_components"] == 3
        assert "threshold" in resp.data["editable_keys"]
        assert "pca_dim" not in resp.data["editable_keys"]


class TestSetSgConfig:
    @pytest.mark.asyncio
    async def test_save_partial_update(self, monkeypatch):
        """只传 threshold 时只更新该字段，且写入 sg 段。"""
        from app.schema.api_schemas import SgConfigRequest
        from app.routes import sg_routes

        captured = {}
        def fake_update(section, values):
            captured["section"] = section
            captured["values"] = values
            return values
        monkeypatch.setattr(sg_routes, "update_config_section", fake_update)

        payload = SgConfigRequest(threshold=0.88)
        resp = await sg_routes.set_sg_config(payload)
        assert resp.code == "ok"
        assert resp.data["saved"] is True
        assert resp.data["updated"] == ["threshold"]
        assert captured["section"] == "sg"
        assert captured["values"] == {"threshold": 0.88}

    @pytest.mark.asyncio
    async def test_save_all_fields(self, monkeypatch):
        from app.schema.api_schemas import SgConfigRequest
        from app.routes import sg_routes

        captured = {}
        monkeypatch.setattr(sg_routes, "update_config_section",
                            lambda section, values: captured.update(values))
        payload = SgConfigRequest(
            threshold=0.8, max_workers=16,
            umap_n_neighbors=30, umap_min_dist=0.05,
        )
        resp = await sg_routes.set_sg_config(payload)
        assert resp.data["saved"] is True
        assert set(resp.data["updated"]) == {
            "threshold", "max_workers", "umap_n_neighbors", "umap_min_dist",
        }
        assert captured["max_workers"] == 16

    @pytest.mark.asyncio
    async def test_empty_payload_no_write(self, monkeypatch):
        """空请求体不应触发写盘。"""
        from app.schema.api_schemas import SgConfigRequest
        from app.routes import sg_routes

        called = []
        monkeypatch.setattr(sg_routes, "update_config_section",
                            lambda section, values: called.append((section, values)))
        resp = await sg_routes.set_sg_config(SgConfigRequest())
        assert resp.data["saved"] is False
        assert called == []


class TestGetSgLatest:
    def test_no_output(self):
        """无产物时返回 no_sg_output。"""
        from app.routes.sg_routes import get_sg_latest

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=None):
            resp = get_sg_latest()
        assert resp.code == "no_sg_output"
        assert resp.data is None

    def test_returns_graph(self, tmp_path):
        """有产物时返回图谱和统计。"""
        from app.routes.sg_routes import get_sg_latest

        graph = {"nodes": [{"id": "A"}, {"id": "B"}], "links": [{"source": "A", "target": "B"}]}
        task_dir = tmp_path / "20260708_120000"
        task_dir.mkdir()

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(graph, task_dir)):
            resp = get_sg_latest()
        assert resp.code == "ok"
        assert resp.data["node_count"] == 2
        assert resp.data["edge_count"] == 1
        assert resp.data["task_dir"] == "20260708_120000"
        assert resp.data["graph"] == graph


class TestBuildSg:
    @pytest.mark.asyncio
    async def test_service_unavailable(self):
        from app.routes.sg_routes import build_sg

        container = _mock_container(sg_service=None)
        resp = await build_sg(container=container)
        assert resp.code == "sg_unavailable"

    @pytest.mark.asyncio
    async def test_build_busy_returns_error_code(self):
        """已有任务运行时 build_async 返回 error，路由应标 sg_busy。"""
        from app.routes.sg_routes import build_sg

        svc = MagicMock()
        svc.build_async = AsyncMock(return_value={
            "error": "已有构建任务在运行", "status": "running", "progress": 30,
        })
        container = _mock_container(sg_service=svc)
        resp = await build_sg(container=container)
        assert resp.code == "sg_busy"
        assert "已有构建" in resp.message

    @pytest.mark.asyncio
    async def test_build_started_returns_snapshot(self):
        from app.routes.sg_routes import build_sg

        svc = MagicMock()
        svc.build_async = AsyncMock(return_value={
            "status": "running", "progress": 0, "message": "启动构建...",
        })
        container = _mock_container(sg_service=svc)
        resp = await build_sg(container=container)
        assert resp.code == "ok"
        assert resp.data["status"] == "running"


class TestSgSearch:
    """测试 /sg/search 路由（从 doc_routes 迁移而来）。"""

    @pytest.mark.asyncio
    async def test_search_empty_query(self):
        """空查询返回空结果。"""
        from app.routes.sg_routes import sg_search

        result = await sg_search(q="", top_k=10)
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_search_no_output(self):
        """没有图谱数据时返回空结果。"""
        from app.routes.sg_routes import sg_search

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=None):
            result = await sg_search(q="test", top_k=10)
        assert result == {"results": []}

    @pytest.mark.asyncio
    async def test_search_keyword_fallback(self):
        """向量索引不存在时回退到关键词搜索。"""
        from app.routes.sg_routes import sg_search

        mock_graph = {"nodes": [
            {"id": "1", "name": "Docker部署", "category": "tech"},
            {"id": "2", "name": "系统架构", "category": "tech"},
        ]}
        mock_task_dir = MagicMock()
        mock_task_dir.__truediv__ = lambda self, x: MagicMock(exists=lambda: False)

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(mock_graph, mock_task_dir)):
            result = await sg_search(q="Docker", top_k=10)
        assert len(result["results"]) > 0
        assert result["results"][0]["title"] == "Docker部署"

    @pytest.mark.asyncio
    async def test_search_vector_path_reads_npz(self, tmp_path):
        """有 .npz 产物 + faiss 索引时走向量检索路径（非关键词回退）。

        覆盖 np.load(allow_pickle=False) 读取 + doc_id 复用向量 + faiss 搜索。
        q 设为某个 doc_id 以复用产物向量，避免依赖 embed_client。
        """
        import numpy as np
        import faiss
        from app.routes.sg_routes import sg_search

        # 构造 3 个文档的归一化向量（L2 单位向量，faiss 用内积/余弦）
        dim = 8
        doc_ids = ["d1", "d2", "d3"]
        rng = np.random.default_rng(42)
        raw = rng.random((3, dim)).astype(np.float32)
        faiss.normalize_L2(raw)

        # 写 .npz 产物（用文件对象，避免 np.savez 自动追加 .npz 扩展名）
        with open(str(tmp_path / "vectors.pkl"), "wb") as f:
            np.savez(
                f,
                doc_ids=np.array(doc_ids),
                raw_vectors=raw,
                pca_vectors=np.zeros((3, 2), dtype=np.float32),
                umap_vectors=np.zeros((3, 3), dtype=np.float32),
                pca_dim=np.array(2),
                umap_n_components=np.array(3),
            )
        # 写 faiss 索引（内积索引，向量已归一化 → 余弦相似度）
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        idx = faiss.IndexFlatIP(dim)
        idx.add(raw)
        faiss.write_index(idx, str(models_dir / "faiss.index"))

        graph = {"nodes": [
            {"id": "d1", "name": "文档一", "category": "tech"},
            {"id": "d2", "name": "文档二", "category": "tech"},
            {"id": "d3", "name": "文档三", "category": "tech"},
        ]}

        with patch("app.services.sg_service.SemanticGraphService.latest_graph",
                   return_value=(graph, tmp_path)):
            # q="d1" 命中 doc_id，复用产物向量，不调 embed_client
            result = await sg_search(q="d1", top_k=10, container=MagicMock())

        assert "results" in result
        # d1 与自身最相似，应出现在结果中
        ids = [r["id"] for r in result["results"]]
        assert "d1" in ids
