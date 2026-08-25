"""Qwen 系模型家族适配器（model_adapter 能力插件）。

宿主（app/agents/model_family_adapters.py）在本进程内 import 本模块，
取模块级 ADAPTERS 注册进家族适配器注册表。
"""
from __future__ import annotations

import re

from app.agents.model_family_adapters import ModelFamilyAdapter


class QwenAdapter(ModelFamilyAdapter):
    """Qwen 系（Qwen3 及后续混合思考版本，含 MLX/Ollama 变体命名）。

    /no_think 软开关由聊天模板处理，以最后一条消息的开关为准，
    system 与 user 都注入（实测 LM Studio qwen3.8-27b-mlx "你好"：
    仅 system 22.6s→12.8s，双注入 22.6s→5.0s，首字 16.3s→3.0s）。
    软开关只缩短思考而非完全消除，max_tokens 预算需为残余思考留余量。
    """

    family = "qwen"
    _match_re = re.compile(r"qwen", re.IGNORECASE)

    def no_think(self, system_text: str, user_text: str) -> tuple[str, str]:
        return f"{system_text}\n/no_think", f"{user_text} /no_think"


ADAPTERS = [QwenAdapter()]
