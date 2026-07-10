from __future__ import annotations

import logging
from typing import Any

from .llm_base_client import LlmBaseClient

logger = logging.getLogger(__name__)


class LlmChatClient(LlmBaseClient):
    def __init__(self, role: str = "chat") -> None:
        super().__init__(role=role)

    async def chat(self, messages: list[dict[str, Any]], timeout: int = 120) -> str:
        # 记录 LLM 调用
        try:
            from ..container import get_container
            metrics = get_container().metrics_service
            metrics.record_llm_call()
        except Exception:
            pass  # 容器未初始化时忽略

        # 思考模式由角色配置决定(设置页可开关),默认关:家居助手不需要先吐推理。
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if self.thinking_enabled:
            payload["thinking"] = {"type": "enabled"}
        try:
            data = await self.post_chat(payload, timeout=timeout)
            choices = data.get("choices") or []
            if choices:
                return choices[0].get("message", {}).get("content", "") or ""
            return ""
        except Exception as e:
            try:
                from ..container import get_container
                metrics = get_container().metrics_service
                metrics.record_llm_call(error=True)
            except Exception:
                pass
            raise
