"""Tests for api_models Pydantic validation."""
from __future__ import annotations

from app.core.api_models import ApiResponse, CameraStateModel, HealthData


class TestApiResponse:
    def test_default_code(self):
        resp = ApiResponse(data={"key": "val"})
        assert resp.code == "ok"
        assert resp.message == "success"

    def test_custom_code(self):
        resp = ApiResponse(code="error", message="fail", data=None)
        assert resp.code == "error"

    def test_model_dump(self):
        resp = ApiResponse(data={"key": "val"})
        d = resp.model_dump()
        assert d["code"] == "ok"
        assert d["data"] == {"key": "val"}


class TestCameraStateModel:
    def test_minimal(self):
        state = CameraStateModel(
            camera_opened=False,
            backend_name="none",
            frame_width=0,
            frame_height=0,
            fps=0.0,
            last_frame_at=0.0,
            last_error=None,
            action="idle",
            feedback="ok",
            details=None,
            confirmed=False,
            model_fps=0.0,
        )
        assert state.action == "idle"
        assert state.infer_busy is False

    def test_defaults(self):
        state = CameraStateModel(
            camera_opened=False, backend_name="n",
            frame_width=0, frame_height=0, fps=0, last_frame_at=0,
            last_error=None, action="idle", feedback="", details=None,
            confirmed=False,
            model_fps=0,
        )
        assert state.motion_distance == -1
        assert state.infer_count == 0


class TestCameraStateModelValidation:
    def test_extends_camera_state(self):
        data = CameraStateModel(
            camera_opened=False, backend_name="n",
            frame_width=0, frame_height=0, fps=0, last_frame_at=0,
            last_error=None, action="idle", feedback="", details=None,
            confirmed=False,
            model_fps=0,
        )
        assert data.action == "idle"
        assert data.infer_busy is False
