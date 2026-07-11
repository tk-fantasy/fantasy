from __future__ import annotations

import asyncio
import base64
import logging
import random
from typing import TYPE_CHECKING, Any

import cv2
import httpx
import numpy as np

from ..core.config import get_config
from ..core.exceptions import VisionInferenceException
from .llm_base_client import LlmBaseClient, _get_shared_client

if TYPE_CHECKING:
    from ..services.api_key_manager import ApiKeyManager

logger = logging.getLogger(__name__)


def downscale_for_vision(frame_bgr: np.ndarray, max_side: int) -> np.ndarray:
    """等比缩到最长边 max_side。视觉模型按 patch 切 token,
    分辨率减半视觉 token 约降为 1/4,语义(有没有人/在做什么)基本无损。"""
    if max_side <= 0:
        return frame_bgr
    height, width = frame_bgr.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return frame_bgr
    scale = max_side / float(longest)
    return cv2.resize(frame_bgr, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)


def encode_frame_b64(frame_bgr: np.ndarray, max_side: int, jpeg_quality: int) -> str:
    small = downscale_for_vision(frame_bgr, max_side)
    ok, encoded = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
    if not ok:
        raise VisionInferenceException("图像编码失败")
    return base64.b64encode(encoded.tobytes()).decode()


def _encode_frames_b64(frames: list[np.ndarray], max_side: int, jpeg_quality: int) -> list[str]:
    """批量编码多帧为 base64。供 evaluate_condition 通过 to_thread 调用，
    避免在事件循环里同步跑 N 次 imencode+base64 持 GIL。"""
    return [encode_frame_b64(f, max_side, jpeg_quality) for f in frames]


class LlmVisionClient(LlmBaseClient):
    def __init__(self) -> None:
        self._key_pool: "ApiKeyManager | None" = None
        super().__init__(role="vision")

    def set_key_pool(self, key_pool: "ApiKeyManager") -> None:
        """注入多 key 池;注入后 evaluate_condition 走池(轮询+并发控制)。"""
        self._key_pool = key_pool

    def _load(self) -> None:
        super()._load()
        self._max_side = int(get_config("vision.downscale_max_side", 448))
        self._jpeg_quality = int(get_config("vision.jpeg_quality", 70))
        self._timeout = int(get_config("llm.vision_timeout_seconds", 30))

    async def classify_frame(self, frame_bgr, timeout: int | None = None, focus: str = "画面中的人和他们的行为") -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "event": "no_event", "feedback": "视觉模型未启用。"}

        prompt_text = (
            "分析画面，只输出 JSON，不要任何额外文字。\n"
            "格式: {\"event\": \"snake_case事件名或no_event\", \"observation\": \"简短中文描述\"}\n"
            f"关注: {focus}\n"
        )
        # OpenAI 多模态格式:有图时 content 为 parts 列表
        if self.multimodal_enabled:
            # imencode + base64 是 CPU 密集操作，持 GIL 会饿死摄像头采集线程。
            # 用 to_thread 挪到独立线程，让事件循环在网络 I/O 等待期间能释放 GIL。
            b64 = await asyncio.to_thread(encode_frame_b64, frame_bgr, self._max_side, self._jpeg_quality)
            content: str | list[dict[str, Any]] = [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]
        else:
            content = prompt_text
        message: dict[str, Any] = {"role": "user", "content": content}
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "max_tokens": 128,
            "temperature": 0.1,
            "messages": [message],
        }
        return await self.post_chat(payload, timeout=timeout or self._timeout)

    async def ask_about_frame(self, frame_bgr, question: str, timeout: int | None = None) -> str:
        """按需问图:把当前帧和用户问题一起送视觉模型,返回自然语言回答。"""
        if not self.enabled:
            return "视觉模型未启用，无法分析画面。"
        if not self.multimodal_enabled:
            return "当前视觉模型已关闭图像输入(多模态),无法分析画面。"
        # imencode + base64 是 CPU 密集操作，持 GIL 会阻塞事件循环；
        # 与 classify_frame 一致，用 to_thread 挪到独立线程。
        image_b64 = await asyncio.to_thread(encode_frame_b64, frame_bgr, self._max_side, self._jpeg_quality)
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "max_tokens": 256,
            "temperature": 0.3,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"请根据这张摄像头画面，用简洁中文回答用户问题。问题：{question}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                    ],
                }
            ],
        }
        response = await self.post_chat(payload, timeout=timeout or self._timeout)
        choices = response.get("choices") or []
        if choices:
            return str(choices[0].get("message", {}).get("content", "") or "").strip() or "视觉模型没有返回内容。"
        return "视觉模型没有返回内容。"

    # ----------------------------------------------- 规则引擎:条件评估

    _CONDITION_SYSTEM_PROMPT = (
        "你是一个家庭管家，查看监控摄像头画面，判断规则条件是否满足。\n\n"
        "## 任务\n"
        "根据 condition 和 current_frames 当前画面，只关注条件是否成立，不做任何无关行为。\n"
        "## 输出（只输出一个数字，不要有任何其他内容）\n"
        "0 = 条件不成立\n"
        "1 = 条件成立\n"
    )

    _CONDITION_PREFIX = "condition: "
    _LAST_HAPPENED_PREFIX = "\nlast_happened_frames - 上次满足条件时的图像序列：\n"

    async def evaluate_condition(
        self,
        frames: list[np.ndarray],
        condition: str,
        timeout: int | None = None,
        context_info: str = "",
        pre_encoded_b64: list[str] | None = None,
    ) -> str:
        """条件评估，返回 "0" 或 "1"。

        - 0: 条件不成立
        - 1: 条件成立

        异步，使用 httpx 连接池并发调用。

        Args:
            context_info: 可选的环境上下文（时间/天气等），拼入提示词帮助模型判断。
            pre_encoded_b64: 调用方预先编码好的 base64 帧列表。传入则跳过本函数内的
                encode_frame_b64（避免一个 tick 内 5 条规则重复编码同一批帧）。
                不传则本函数内部编码（编码走 to_thread，不阻塞事件循环/GIL）。
        """
        if not condition.strip():
            return "0"
        if pre_encoded_b64 is not None:
            current_b64_list = pre_encoded_b64
        else:
            if not self.enabled or not self.multimodal_enabled or not frames:
                return "0"
            current_b64_list = await asyncio.to_thread(
                _encode_frames_b64, frames, self._max_side, self._jpeg_quality
            )
        if not current_b64_list:
            return "0"

        # 构建 user_content：环境上下文 + 条件
        if context_info:
            user_content = (
                f"当前环境信息：\n{context_info}\n\n"
                f"current_frames - 当前图像序列：\n"
                f"{self._CONDITION_PREFIX}{condition.strip()}"
            )
        else:
            user_content = f"current_frames - 当前图像序列：\n{self._CONDITION_PREFIX}{condition.strip()}"

        logger.info("evaluate_condition sending %d images", len(current_b64_list))

        if self._key_pool is not None and self._key_pool.available:
            entry = await self._key_pool.acquire(timeout=float(timeout or self._timeout))
            if entry is None:
                logger.warning("evaluate_condition: key pool busy, no slot")
                return "0"
            ok = False
            try:
                content_parts = [
                    {"type": "text", "text": user_content},
                    *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}} for b64 in current_b64_list],
                ]
                content, ok = await self._post_to_entry_async(
                    entry=entry,
                    content_parts=content_parts,
                    timeout=timeout or self._timeout,
                    system_prompt=self._CONDITION_SYSTEM_PROMPT,
                )
                return content
            finally:
                await self._key_pool.release(entry, success=ok)

        # 无池:回退基类单 key（异步）
        content_parts = [
            {"type": "text", "text": user_content},
            *[{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}} for b64 in current_b64_list],
        ]
        messages = [
            {"role": "system", "content": self._CONDITION_SYSTEM_PROMPT},
            {"role": "user", "content": content_parts},
        ]
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "max_tokens": 16,
            "temperature": 0.1,
            "messages": messages,
        }
        response = await self.post_chat(payload, timeout=timeout or self._timeout)
        choices = response.get("choices") or []
        if choices:
            return str(choices[0].get("message", {}).get("content", "") or "").strip()
        return "0"

    @staticmethod
    async def _post_to_entry_async(
        entry: dict,
        content_parts: list[dict],
        timeout: int,
        system_prompt: str | None = None,
        max_retries: int = 2,
    ) -> tuple[str, bool]:
        """用 key 池直接发 OpenAI 兼容请求（异步），返回 (content, ok)。

        ok=True 表示请求成功（含"成功但无 choices"的情况，content 为 "0"）；
        ok=False 表示请求失败（网络异常 / 非 2xx / 重试耗尽），调用方据此上报 key 池故障。
        """
        url = f"{entry['base_url']}{entry['chat_path']}"
        headers = {"Content-Type": "application/json"}
        if entry.get("api_key"):
            headers["Authorization"] = f"Bearer {entry['api_key']}"
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content_parts})
        payload = {
            "model": entry["model"],
            "stream": False,
            "max_tokens": 16,
            "temperature": 0.1,
            "messages": messages,
        }
        client = _get_shared_client()
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 429 and attempt < max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    return "0", True
                return str(choices[0].get("message", {}).get("content", "")).strip(), True
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(0.3 * (2 ** attempt) + random.uniform(0, 0.05))
                    continue
                logger.warning("evaluate_condition request failed after %d retries: %s", max_retries, exc)
                return "0", False
            except httpx.HTTPError as exc:
                logger.warning("evaluate_condition request failed: %s", exc)
                return "0", False
            except ValueError:
                return "0", False
        logger.warning("evaluate_condition exhausted retries: %s", last_exc)
        return "0", False
