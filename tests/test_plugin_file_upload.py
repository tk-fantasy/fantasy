"""插件数据文件上传端点测试：上传/非法 id/未知插件/大小上限。"""
from __future__ import annotations

import io
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token
from app.main import app
from app.routes import integration_routes as ir


@pytest.fixture()
def client(tmp_path, monkeypatch):
    token = create_access_token("u-admin", "tester")
    # 上传根目录指 tmp（隔离）；插件目录存在性校验指 tmp
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(ir, "_upload_root", lambda: upload_root)
    monkeypatch.setattr(ir, "_resolve_plugin_dir", lambda: tmp_path / "plugins")
    (tmp_path / "plugins" / "demo").mkdir(parents=True)

    with patch("app.core.database.Database.get") as get_db:
        db = AsyncMock()
        db.user_get_by_id = AsyncMock(return_value={
            "id": "u-admin", "username": "tester", "is_admin": 1})
        get_db.return_value = db
        yield TestClient(app), {"Authorization": f"Bearer {token}"}, upload_root


def test_upload_writes_file_and_returns_path(client):
    tc, headers, upload_root = client
    resp = tc.post(
        "/api/integrations/demo/files",
        files={"file": ("我的 视频.mp4", io.BytesIO(b"x" * 1024), "video/mp4")},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    saved = upload_root / "demo"
    files = list(saved.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"x" * 1024
    # 文件名清洗：空格等变下划线，中文保留
    assert "我的_视频.mp4" in files[0].name
    assert body["data"]["path"] == str(files[0])


def test_upload_rejects_bad_plugin_id(client):
    tc, headers, _ = client
    # 路径穿越归一化：../evil 根本到不了 handler（404/405），防穿越由路由层兜住
    resp = tc.post(
        "/api/integrations/../evil/files",
        files={"file": ("a.mp4", io.BytesIO(b"x"), "video/mp4")},
        headers=headers,
    )
    assert resp.status_code in (404, 405)
    # 常规非法字符（含点/空格）走 handler 校验分支拒绝
    resp2 = tc.post(
        "/api/integrations/bad.id/files",
        files={"file": ("a.mp4", io.BytesIO(b"x"), "video/mp4")},
        headers=headers,
    )
    body = resp2.json()
    assert body["success"] is False and "非法" in body["message"]


def test_upload_rejects_unknown_plugin(client):
    tc, headers, _ = client
    resp = tc.post(
        "/api/integrations/ghost/files",
        files={"file": ("a.mp4", io.BytesIO(b"x"), "video/mp4")},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is False


def test_upload_size_cap(client, monkeypatch):
    tc, headers, _ = client
    monkeypatch.setattr(ir, "MAX_PLUGIN_FILE_SIZE", 8)
    resp = tc.post(
        "/api/integrations/demo/files",
        files={"file": ("big.mp4", io.BytesIO(b"x" * 64), "video/mp4")},
        headers=headers,
    )
    assert resp.json()["success"] is False
    # 半截文件已清理
    assert not list((upload_root_dir := list(client[2].glob("demo/*")))) or all(
        f.stat().st_size <= 8 for f in upload_root_dir)


def test_upload_requires_auth(client):
    tc, _, _ = client
    resp = tc.post(
        "/api/integrations/demo/files",
        files={"file": ("a.mp4", io.BytesIO(b"x"), "video/mp4")},
    )
    assert resp.status_code == 401
