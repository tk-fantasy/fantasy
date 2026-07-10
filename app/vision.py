"""视觉识别结果数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionResult:
    """视觉推理结果。"""
    action: str
    feedback: str
    details: dict = field(default_factory=dict)
