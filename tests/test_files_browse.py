"""/api/files/browse 目录浏览端点测试：盘符列表/列目录/扩展名过滤/非法路径。

走完整 HTTP 链路（含全局 token 中间件）：用 create_access_token 签发真 token，
mock Database.user_get_by_id 让签发用户是管理员（get_current_admin 查库验身份）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from app.core.auth import create_access_token
from app.main import app


@pytest.fixture()
def client():
    token = create_access_token("u-admin", "tester")
    with patch("app.core.database.Database.get") as get_db:
        db = AsyncMock()
        db.user_get_by_id = AsyncMock(return_value={
            "id": "u-admin", "username": "tester", "is_admin": 1,
        })
        get_db.return_value = db
        yield TestClient(app), {"Authorization": f"Bearer {token}"}


def test_browse_root_lists_drives_or_slash(client):
    tc, headers = client
    resp = tc.get("/api/files/browse?path=", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["path"] == ""
    assert isinstance(data["entries"], list) and data["entries"]
    assert all(e["type"] == "dir" for e in data["entries"])


def test_browse_requires_auth(client):
    tc, _ = client
    resp = tc.get("/api/files/browse?path=")
    assert resp.status_code == 401


def test_browse_directory_filters_video_exts(client, tmp_path):
    tc, headers = client
    (tmp_path / "subdir").mkdir()
    (tmp_path / "a.mp4").write_bytes(b"x" * 10)
    (tmp_path / "b.MOV").write_bytes(b"x" * 20)
    (tmp_path / "c.txt").write_text("nope")

    resp = tc.get("/api/files/browse", params={"path": str(tmp_path)}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    names = {e["name"] for e in data["entries"]}
    # 目录在前 + 视频文件（大小写扩展名都认），txt 被过滤
    assert names == {"subdir", "a.mp4", "b.MOV"}
    files = {e["name"]: e for e in data["entries"] if e["type"] == "file"}
    assert files["a.mp4"]["size"] == 10
    assert data["parent"]  # 有上级


def test_browse_custom_exts(client, tmp_path):
    tc, headers = client
    (tmp_path / "x.txt").write_text("t")
    (tmp_path / "y.mp4").write_bytes(b"x")
    resp = tc.get("/api/files/browse", params={"path": str(tmp_path), "exts": "txt"},
                  headers=headers)
    data = resp.json()["data"]
    names = {e["name"] for e in data["entries"]}
    assert "x.txt" in names and "y.mp4" not in names


def test_browse_invalid_path_rejected(client, tmp_path):
    tc, headers = client
    fake_file = tmp_path / "notadir"
    fake_file.write_text("f")
    resp = tc.get("/api/files/browse", params={"path": str(fake_file)}, headers=headers)
    assert resp.status_code == 400
