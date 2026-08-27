"""/api/emoji/* 路由 HTTP 层测试（此前仅 emoji_service 服务层有测试）。

覆盖：search 三态（loaded/loading/not_loaded）、偏好 CRUD 走真 Database mock、
rebuild 的鉴权/409/400/成功路径。容器注入 monkeypatch 模块级 get_container；
rebuild 的后台任务管理器替换为空转桩，避免真实 spawn。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.container import get_container
from app.core.auth import create_access_token
from app.main import app


class _DummyTaskMgr:
    """吞掉 spawn 的协程，避免 un-awaited warning。"""

    def __init__(self):
        self.spawned = []

    def spawn(self, coro, name=None):
        self.spawned.append(name)
        coro.close()


def _auth():
    return {"Authorization": f"Bearer {create_access_token('u1', 'tester')}"}


@pytest.fixture()
def client():
    yield TestClient(app)
    app.dependency_overrides.pop(get_container, None)


def _patch_container(service=None, embed_enabled=False):
    """把伪造容器绑进依赖覆盖。"""
    cont = MagicMock()
    cont.emoji_service = service
    embed = MagicMock()
    embed.enabled = embed_enabled
    cont.embed_client = embed
    app.dependency_overrides[get_container] = lambda: cont
    return cont


def _emoji_service(loaded=True, loading=False, running=False):
    svc = MagicMock()
    svc.is_loaded = loaded
    svc.is_loading = loading
    svc.rebuild_status = {"running": running, "done": 0, "total": 0}
    svc.search = AsyncMock(return_value=[{"char": "😀", "score": 0.9}])
    return svc


# --------------- /emoji/search ---------------

def test_search_returns_results_when_loaded(client, monkeypatch):
    svc = _emoji_service(loaded=True)
    _patch_container( service=svc)

    resp = client.get("/api/emoji/search", params={"q": "开心"}, headers=_auth())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "ok"
    assert data["results"] == [{"char": "😀", "score": 0.9}]
    svc.search.assert_awaited_once_with("开心", top_k=20)


def test_search_reports_loading_state_without_searching(client, monkeypatch):
    svc = _emoji_service(loaded=False, loading=True)
    _patch_container( service=svc)

    data = client.get("/api/emoji/search", params={"q": "x"}, headers=_auth()).json()["data"]
    assert data["status"] == "loading"
    assert data["results"] == []
    svc.search.assert_not_awaited()


def test_search_not_loaded_state(client, monkeypatch):
    svc = _emoji_service(loaded=False, loading=False)
    _patch_container( service=svc)

    data = client.get("/api/emoji/search", params={"q": "x"}, headers=_auth()).json()["data"]
    assert data["status"] == "not_loaded"
    svc.search.assert_not_awaited()


def test_search_requires_q_param(client, monkeypatch):
    _patch_container( service=_emoji_service())
    resp = client.get("/api/emoji/search", headers=_auth())
    assert resp.status_code == 422


# --------------- /emoji/preferences ---------------

def test_preferences_require_auth(client):
    """偏好端点没有路由级依赖，仅靠全局 api_token_guard 守门。"""
    assert client.get("/api/emoji/preferences").status_code == 401
    assert client.put("/api/emoji/preferences", json={"scope": "s", "key": "k", "emoji_char": "e"}).status_code == 401

def test_get_preferences_reads_db(client):
    db = AsyncMock()
    db.emoji_prefs_all = AsyncMock(return_value=[{"scope": "chat", "key": "greet", "emoji_char": "😀"}])
    with patch("app.core.database.Database.get", return_value=db):
        resp = client.get("/api/emoji/preferences", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["data"][0]["emoji_char"] == "😀"
    db.emoji_prefs_all.assert_awaited_once()


def test_put_preference_persists(client):
    db = AsyncMock()
    payload = {"scope": "chat", "key": "morning", "emoji_char": "☀️"}
    with patch("app.core.database.Database.get", return_value=db):
        resp = client.put("/api/emoji/preferences", json=payload, headers=_auth())
    assert resp.status_code == 200
    db.emoji_pref_upsert.assert_awaited_once_with("chat", "morning", "☀️")
    assert resp.json()["data"]["emoji_char"] == "☀️"


def test_put_preference_missing_field_rejected(client):
    db = AsyncMock()
    bad = {"scope": "", "key": "k", "emoji_char": "e"}
    with patch("app.core.database.Database.get", return_value=db):
        resp = client.put("/api/emoji/preferences", json=bad, headers=_auth())
    assert resp.status_code == 400
    assert resp.json()["code"] == "missing_params"
    db.emoji_pref_upsert.assert_not_awaited()


def test_delete_preference_echoes_result(client):
    db = AsyncMock()
    db.emoji_pref_delete = AsyncMock(return_value=True)
    with patch("app.core.database.Database.get", return_value=db):
        resp = client.delete("/api/emoji/preferences/chat/morning", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deleted"] is True and data["scope"] == "chat" and data["key"] == "morning"
    db.emoji_pref_delete.assert_awaited_once_with("chat", "morning")


# --------------- /emoji/rebuild + status ---------------

def test_rebuild_requires_auth(client, monkeypatch):
    _patch_container( service=_emoji_service())
    assert client.post("/api/emoji/rebuild").status_code == 401


def test_rebuild_conflict_while_running(client, monkeypatch):
    _patch_container( service=_emoji_service(running=True))
    resp = client.post("/api/emoji/rebuild", headers=_auth())
    assert resp.status_code == 409
    assert resp.json()["code"] == "rebuild_in_progress"


def test_rebuild_rejected_without_embed(client, monkeypatch):
    _patch_container( service=_emoji_service(), embed_enabled=False)
    resp = client.post("/api/emoji/rebuild", headers=_auth())
    assert resp.status_code == 400
    assert resp.json()["code"] == "embed_not_configured"


def test_rebuild_starts_background_task(client, monkeypatch):
    # rebuild 内部 from ..main import _background_task_mgr 是请求期取值，可安全替换
    svc = _emoji_service()
    svc.rebuild_index = AsyncMock(return_value=None)
    _patch_container( service=svc, embed_enabled=True)

    dummy = _DummyTaskMgr()
    monkeypatch.setattr("app.main._background_task_mgr", dummy)

    resp = client.post("/api/emoji/rebuild", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "started"
    assert dummy.spawned == ["emoji_rebuild"]


def test_rebuild_status_requires_auth_and_returns_dict(client, monkeypatch):
    _patch_container( service=_emoji_service(running=True))

    assert client.get("/api/emoji/rebuild/status").status_code == 401

    resp = client.get("/api/emoji/rebuild/status", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["data"]["running"] is True
