from __future__ import annotations


class AppException(Exception):
    def __init__(self, message: str, code: str = "app_error", http_status: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class VisionInferenceException(AppException):
    def __init__(self, message: str = "视觉识别异常") -> None:
        super().__init__(message, code="vision_inference_error", http_status=502)


class ModelServiceException(AppException):
    def __init__(self, message: str = "模型服务异常") -> None:
        super().__init__(message, code="model_service_error", http_status=502)


