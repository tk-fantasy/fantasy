"""Tests for exception handlers using FastAPI TestClient."""
from __future__ import annotations

from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient

from app.core.exceptions import AppException
from app.utils.handlers import register_exception_handlers


def _make_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/app-error")
    def raise_app_error():
        raise AppException("test error", code="test_code", http_status=400)

    @app.get("/unexpected")
    def raise_unexpected():
        raise RuntimeError("boom")

    @app.get("/ok")
    def ok():
        return {"status": "ok"}

    return app


class TestExceptionHandlers:
    def setup_method(self):
        self.app = _make_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def test_app_exception(self):
        resp = self.client.get("/app-error")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "test_code"
        assert body["message"] == "test error"

    def test_unexpected_exception(self):
        resp = self.client.get("/unexpected")
        assert resp.status_code == 500
        body = resp.json()
        assert body["code"] == "internal_error"

    def test_normal_response(self):
        resp = self.client.get("/ok")
        assert resp.status_code == 200
