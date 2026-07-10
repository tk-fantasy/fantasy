from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: str = Field(default="ok")
    message: str = Field(default="success")
    data: T


class CameraStateModel(BaseModel):
    camera_opened: bool
    backend_name: str
    frame_width: int
    frame_height: int
    fps: float
    worker_fps: float = 0.0
    last_frame_at: float
    last_error: str | None
    action: str
    feedback: str
    details: dict[str, Any] | None
    confirmed: bool
    model_fps: float
    motion_distance: int = -1
    motion_threshold: int = 5
    last_infer_at: float = 0.0
    infer_count: int = 0
    infer_busy: bool = False


class HealthData(BaseModel):
    status: str
    llm_model: str
    llm_enabled: bool
    camera: CameraStateModel
    log_file: str
    ha_available: bool = False
    llm_available: bool = False


