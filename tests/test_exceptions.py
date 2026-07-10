"""Tests for app.exceptions — pure classes, no mocking."""
from __future__ import annotations

from app.core.exceptions import (
    AppException,
    ModelServiceException,
    VisionInferenceException,
)


class TestAppException:
    def test_default_values(self):
        e = AppException("test error")
        assert str(e) == "test error"
        assert e.message == "test error"
        assert e.code == "app_error"
        assert e.http_status == 500

    def test_custom_values(self):
        e = AppException("not found", code="nf", http_status=404)
        assert e.code == "nf"
        assert e.http_status == 404

    def test_is_exception(self):
        assert issubclass(AppException, Exception)


class TestSubclasses:
    def test_vision_inference_exception(self):
        e = VisionInferenceException()
        assert e.code == "vision_inference_error"
        assert e.http_status == 502

    def test_model_service_exception(self):
        e = ModelServiceException()
        assert e.code == "model_service_error"
        assert e.http_status == 502

    def test_all_are_app_exception_subclass(self):
        for cls in [VisionInferenceException, ModelServiceException]:
            assert issubclass(cls, AppException)
