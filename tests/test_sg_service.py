"""Tests for app/services/sg_service.py — 语义图构建服务（状态机 + 产物查询）。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.sg_service import SemanticGraphService


class TestSnapshot:
    def test_initial_snapshot(self):
        svc = SemanticGraphService(embed_client=MagicMock(), llm_chat_client=MagicMock())
        snap = svc.snapshot()
        assert snap["status"] == "idle"
        assert snap["progress"] == 0
        assert snap["message"] == ""
        assert snap["task_dir"] is None

    def test_snapshot_reflects_state(self):
        svc = SemanticGraphService(embed_client=MagicMock(), llm_chat_client=MagicMock())
        svc.status = "running"
        svc.progress = 42
        svc.message = "向量化中"
        svc.task_dir = Path("/tmp/sg_task")
        snap = svc.snapshot()
        assert snap["status"] == "running"
        assert snap["progress"] == 42
        assert snap["message"] == "向量化中"
        assert "sg_task" in snap["task_dir"]


class TestCancel:
    def test_cancel_sets_flag(self):
        svc = SemanticGraphService(embed_client=MagicMock(), llm_chat_client=MagicMock())
        assert svc._cancel is False
        svc.cancel()
        assert svc._cancel is True
        assert "取消" in svc.message


class TestLatestGraph:
    def test_no_output_root(self, tmp_path, monkeypatch):
        """OUTPUT_ROOT 不存在时返回 None。"""
        monkeypatch.setattr(
            "app.services.sg_service.OUTPUT_ROOT", tmp_path / "nonexistent"
        )
        assert SemanticGraphService.latest_graph() is None

    def test_empty_output_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.services.sg_service.OUTPUT_ROOT", tmp_path)
        assert SemanticGraphService.latest_graph() is None

    def test_returns_most_recent(self, tmp_path, monkeypatch):
        """多个产物时按 mtime 选最近的。"""
        import os
        import time
        monkeypatch.setattr("app.services.sg_service.OUTPUT_ROOT", tmp_path)

        old_dir = tmp_path / "20260101_000000"
        old_dir.mkdir()
        (old_dir / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "old"}], "links": []}),
            encoding="utf-8",
        )
        # 显式把 old_dir 的 mtime 设为 1 天前，避免 Windows 粗粒度计时器
        # 导致两个同秒创建的目录顺序不确定。
        old_time = time.time() - 86400
        os.utime(old_dir, (old_time, old_time))

        new_dir = tmp_path / "20260708_120000"
        new_dir.mkdir()
        (new_dir / "graph.json").write_text(
            json.dumps({"nodes": [{"id": "new"}], "links": []}),
            encoding="utf-8",
        )

        graph, task_dir = SemanticGraphService.latest_graph()
        assert task_dir.name == "20260708_120000"
        assert graph["nodes"][0]["id"] == "new"

    def test_skips_dir_without_graph_json(self, tmp_path, monkeypatch):
        """没有 graph.json 的目录应被跳过。"""
        monkeypatch.setattr("app.services.sg_service.OUTPUT_ROOT", tmp_path)

        empty_dir = tmp_path / "20260101_000000"
        empty_dir.mkdir()
        # 只有 vectors.pkl，没有 graph.json
        (empty_dir / "vectors.pkl").write_text("x", encoding="utf-8")

        good_dir = tmp_path / "20260102_000000"
        good_dir.mkdir()
        (good_dir / "graph.json").write_text(
            json.dumps({"nodes": [], "links": []}), encoding="utf-8"
        )

        graph, task_dir = SemanticGraphService.latest_graph()
        assert task_dir.name == "20260102_000000"

    def test_skips_corrupt_graph_json(self, tmp_path, monkeypatch):
        """损坏的 graph.json 应被跳过，继续找下一个。"""
        monkeypatch.setattr("app.services.sg_service.OUTPUT_ROOT", tmp_path)

        corrupt = tmp_path / "20260101_000000"
        corrupt.mkdir()
        (corrupt / "graph.json").write_text("not json {{{", encoding="utf-8")

        good = tmp_path / "20260102_000000"
        good.mkdir()
        (good / "graph.json").write_text(
            json.dumps({"nodes": [], "links": []}), encoding="utf-8"
        )

        graph, task_dir = SemanticGraphService.latest_graph()
        assert task_dir.name == "20260102_000000"


class TestBindLoop:
    def test_bind_loop_stores_loop(self):
        import asyncio
        svc = SemanticGraphService(embed_client=MagicMock(), llm_chat_client=MagicMock())
        assert svc._loop is None
        loop = asyncio.new_event_loop()
        svc.bind_loop(loop)
        assert svc._loop is loop
        loop.close()
