"""/api/vision-logs GET/DELETE 端点测试。

/files/browse（同文件另一组端点）已有 test_files_browse.py 覆盖，此处只补
识别日志查询/清空两个端点的 HTTP 层：管理员鉴权 + 查询参数透传。
鉴权模式与 test_files_browse 相同：签发真 token + mock Database 让用户是管理员。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token
from app.main import app


@pytest.fixture()
def env():
    """管理员 token + Database.get 统一桩：鉴权查库与业务查询走同一 mock。"""
    db = AsyncMock()
    def _by_id(uid):
        if uid == "u-admin":
            return {"id": "u-admin", "username": "tester", "is_admin": 1}
        return {"id": uid, "username": "normal", "is_admin": 0}

    db.user_get_by_id = AsyncMock(side_effect=_by_id)
    db.vision_logs_tail = AsyncMock(return_value=[{"id": 2, "kind": "preview"}, {"id": 1, "kind": "action"}])
    db.vision_logs_delete_camera = AsyncMock(return_value=42)

    token = create_access_token("u-admin", "tester")
    headers = {"Authorization": f"Bearer {token}"}
    with patch("app.core.database.Database.get", return_value=db):
        yield TestClient(app), headers, db


def test_list_requires_auth(env):
    tc, _, _ = env
    assert tc.get("/api/vision-logs").status_code == 401


def test_list_requires_admin(env):
    tc, _, _ = env
    non_admin_token = create_access_token("u-normal", "normal")
    resp = tc.get("/api/vision-logs", headers={"Authorization": f"Bearer {non_admin_token}"})
    assert resp.status_code == 403


def test_list_returns_rows_with_default_filters(env):
    tc, headers, db = env
    resp = tc.get("/api/vision-logs", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert [r["id"] for r in rows] == [2, 1]
    db.vision_logs_tail.assert_awaited_once_with(camera_id="", kind="", limit=100)


def test_list_forwards_query_filters(env):
    tc, headers, db = env
    resp = tc.get(
        "/api/vision-logs",
        params={"camera_id": "cam_a", "kind": "rule_eval", "limit": 7},
        headers=headers,
    )
    assert resp.status_code == 200
    db.vision_logs_tail.assert_awaited_once_with(camera_id="cam_a", kind="rule_eval", limit=7)


def test_clear_requires_admin(env):
    tc, _, _ = env
    resp = tc.delete("/api/vision-logs")
    assert resp.status_code == 401


def test_clear_returns_deleted_count(env):
    tc, headers, db = env
    resp = tc.delete("/api/vision-logs?camera_id=cam_a", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": 42}
    db.vision_logs_delete_camera.assert_awaited_once_with("cam_a")
