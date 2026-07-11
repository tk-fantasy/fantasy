from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

import httpx

from ..core.config import get_config
from ..core.exceptions import ModelServiceException
from ..core.key_resolver import resolve_key_for_role
from .http_client import new_client

logger = logging.getLogger(__name__)

# 模块级共享 httpx 客户端：复用连接池，省掉 per-call 新建+关闭的 ~190ms 开销
# (含 TCP/TLS 握手)。httpx AsyncClient 不绑定创建时的循环，跨 asyncio.run 使用安全。
# 生命周期随进程，应用关闭时由 close_shared_client() 清理。
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """获取(懒初始化)共享 httpx 客户端。已关闭则重建。"""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = new_client(timeout=30.0)
    return _shared_client


async def close_shared_client() -> None:
    """应用关闭时清理共享客户端，释放连接池。"""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


class LlmBaseClient:
    """LLM 客户端基类，统一使用 OpenAI 兼容接口。

    配置优先级：
    1. 从 providers.<role>.key_id 获取 key ID
    2. 从 llm_keys 中查找对应的 key 配置
    3. 使用 key 配置中的 base_url, model, api_key
    """

    def __init__(self, role: str = "chat") -> None:
        self._role = role
        self._load()

    def _load(self) -> None:
        """从当前 CONFIG/env 解析全部后端参数。reload() 时重跑以热生效。"""
        key_entry = resolve_key_for_role(self._role)

        if key_entry:
            self._base_url = key_entry.get("base_url", "").rstrip("/")
            self._model = key_entry.get("model", "")
            self._chat_path = key_entry.get("chat_path", "/chat/completions")
            self._embed_path = key_entry.get("embed_path", "/embeddings")
            self._api_key = key_entry.get("api_key", "")
        else:
            self._base_url = ""
            self._model = ""
            self._chat_path = "/chat/completions"
            self._embed_path = "/embeddings"
            self._api_key = ""

        self._enabled = self._resolve_enabled()

    def reload(self) -> None:
        """运行时重读配置（设置页切换模型后调用），无需重启进程。"""
        self._load()

    @property
    def thinking_enabled(self) -> bool:
        """该角色是否开启思考模式。"""
        value = self._role_cfg("thinking")
        return bool(value) if value is not None else False

    @property
    def multimodal_enabled(self) -> bool:
        """多模态（是否发送图像）。"""
        value = self._role_cfg("multimodal")
        return True if value is None else bool(value)

    def _role_cfg(self, key: str) -> Any:
        """获取 providers.<role>.<key>，回退 providers.default.<key>。"""
        value = get_config(f"providers.{self._role}.{key}")
        if value is None:
            value = get_config(f"providers.default.{key}")
        return value

    def _resolve_enabled(self) -> bool:
        """检查是否启用。"""
        env_key = f"LLM_{self._role.upper()}_ENABLED"
        if env_key in os.environ:
            return os.environ[env_key] == "1"
        role_value = self._role_cfg("enabled")
        if role_value is not None:
            return bool(role_value)
        if "LLM_ENABLED" in os.environ:
            return os.getenv("LLM_ENABLED", "0") == "1"
        return bool(get_config("llm.enabled", False))

    @property
    def model(self) -> str:
        return self._model

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def post_json(self, path: str, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
        if not self._enabled:
            raise ModelServiceException("LLM 未启用")
        max_retries = int(get_config("providers.default.max_retries", 2))
        last_exc: Exception | None = None
        client = _get_shared_client()
        for attempt in range(max_retries + 1):
            try:
                response = await client.post(
                    f"{self._base_url}{path}",
                    json=payload,
                    headers=self._headers(),
                    timeout=timeout,
                )
                if response.status_code == 429 and attempt < max_retries:
                    backoff = 0.5 * (2 ** attempt) + random.uniform(0, 0.1)
                    logger.warning("LLM rate limited (429), retry in %.1fs", backoff,
                                   extra={"role": self._role, "attempt": attempt})
                    await asyncio.sleep(backoff)
                    continue
                if response.status_code >= 400:
                    logger.error("LLM %d response body: %s", response.status_code, response.text[:500],
                                 extra={"role": self._role})
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                # 共享客户端若意外关闭,重建一次重试
                if client.is_closed:
                    client = _get_shared_client()
                last_exc = exc
                if attempt < max_retries and isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
                    await asyncio.sleep(0.3 * (2 ** attempt) + random.uniform(0, 0.05))
                    continue
                logger.exception("LLM request failed", extra={"role": self._role})
                raise ModelServiceException(f"LLM 请求失败: {exc}") from exc
            except ValueError as exc:
                logger.exception("LLM response was not valid JSON")
                raise ModelServiceException("LLM 响应不是有效 JSON") from exc
        raise ModelServiceException(f"LLM 请求失败（重试 {max_retries} 次后）: {last_exc}")

    async def post_chat(self, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
        """统一聊天入口。"""
        return await self.post_json(self._chat_path, payload, timeout=timeout)

    async def post_embedding(self, payload: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
        """统一 embedding 入口。"""
        openai_payload = {
            "model": payload["model"],
            "input": payload.get("prompt", ""),
        }
        data = await self.post_json(self._embed_path, openai_payload, timeout=timeout)
        embeddings_data = data.get("data", [])
        if embeddings_data:
            return {"embedding": embeddings_data[0].get("embedding", [])}
        raise ModelServiceException("Embedding 响应格式错误")

    async def post_embeddings_batch(self, texts: list[str], timeout: int = 60) -> list[list[float]]:
        """批量 embedding：一次请求拿多条向量。用于 RAG 索引构建。

        对标 OpenAI / SiliconFlow 的 input 数组形式：
            {"model": "...", "input": ["文本1", "文本2", ...]}
        返回的向量按 API 的 index 字段排序，保证与输入顺序一致。
        """
        if not texts:
            return []
        openai_payload = {
            "model": self._model,
            "input": texts,
        }
        data = await self.post_json(self._embed_path, openai_payload, timeout=timeout)
        embeddings_data = data.get("data", [])
        if len(embeddings_data) != len(texts):
            raise ModelServiceException(
                f"Embedding 批量响应数量不匹配: 请求 {len(texts)}, 返回 {len(embeddings_data)}"
            )
        # 按 index 排序，确保返回顺序与输入一致
        embeddings_data.sort(key=lambda d: d.get("index", 0))
        return [d.get("embedding", []) for d in embeddings_data]
