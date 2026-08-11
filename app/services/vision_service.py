"""视觉分析服务。"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ..clients.llm_vision_client import LlmVisionClient
from ..core.exceptions import VisionInferenceException
from ..utils.json_extractor import extract_json_from_content
from ..vision import ActionResult

logger = logging.getLogger(__name__)


def _feedback_default(event: str) -> str:
    """为常见事件生成简短默认反馈。"""
    mapping = {
        "person_waving": "识别到挥手动作。",
        "person_smiling": "识别到微笑。",
        "person_detected": "画面中检测到人。",
        "multiple_people": "画面中检测到多人。",
        "pet_detected": "画面中检测到宠物。",
        "object_moved": "检测到物体移动。",
        "no_event": "画面平静，无明显事件。",
    }
    return mapping.get(event, f"检测到事件: {event}")


class VisionService:
    def __init__(self, client: LlmVisionClient | None = None) -> None:
        self._client = client or LlmVisionClient()
        # Task 2:per-camera focuses —— 按摄像头分桶,各路隔离。
        # 空串桶(camera_id="")为全局/向后兼容(单摄像头时代无 camera_id)。
        self._vision_focuses: dict[str, list[dict]] = {}

    @property
    def model(self) -> str:
        return self._client.model

    @property
    def enabled(self) -> bool:
        return self._client.enabled

    def set_key_pool(self, key_pool) -> None:
        """把多 key 池透传给底层 client(供条件评估走池)。"""
        self._client.set_key_pool(key_pool)

    async def encode_frames_b64(self, frames: list) -> list[str]:
        """批量编码帧为 base64，供一个 tick 内多条规则复用同一份编码。

        走 asyncio.to_thread，不在事件循环里持 GIL。automation_service 在视觉
        评估分支开头调一次，把结果透传给每条规则的 evaluate_condition(pre_encoded_b64=...)，
        避免 N 条规则各自重复编码同一批帧。
        """
        from ..clients.llm_vision_client import _encode_frames_b64
        client = self._client
        return await asyncio.to_thread(
            _encode_frames_b64, frames, client._max_side, client._jpeg_quality
        )

    # ---- 多条 focus CRUD(per-camera,Task 2)----

    def get_vision_focuses(self, camera_id: str = "") -> list[dict]:
        """获取指定摄像头的视觉关注项。"""
        return list(self._vision_focuses.get(camera_id, []))

    def get_all_focuses_flat(self) -> list[dict]:
        """所有摄像头的关注项拍平返回(KV 持久化用)。

        每条已含 camera_id 字段,load_focuses 可按 camera_id 重新分桶。
        """
        result = []
        for bucket in self._vision_focuses.values():
            result.extend(bucket)
        return result

    def add_focus(self, text: str, camera_id: str = "") -> dict:
        """新增一条视觉关注,归属指定摄像头。"""
        item = {
            "id": uuid.uuid4().hex[:8],
            "text": text.strip(),
            "enabled": True,
            "camera_id": camera_id,
        }
        self._vision_focuses.setdefault(camera_id, []).append(item)
        return item

    def update_focus(self, focus_id: str, *, text: str | None = None,
                     enabled: bool | None = None, camera_id: str = "") -> dict | None:
        """更新指定摄像头下的一条视觉关注。"""
        for item in self._vision_focuses.get(camera_id, []):
            if item["id"] == focus_id:
                if text is not None:
                    item["text"] = text.strip()
                if enabled is not None:
                    item["enabled"] = enabled
                return item
        return None

    def delete_focus(self, focus_id: str, camera_id: str = "") -> bool:
        """删除指定摄像头下的一条视觉关注。"""
        bucket = self._vision_focuses.get(camera_id, [])
        before = len(bucket)
        self._vision_focuses[camera_id] = [f for f in bucket if f["id"] != focus_id]
        return len(self._vision_focuses[camera_id]) < before

    def load_focuses(self, focuses: list[dict]) -> None:
        """从持久化数据加载全部关注项,按 camera_id 分桶。

        KV 存的是扁平 list,每条已含 camera_id 字段;这里按 camera_id 重新分桶。
        """
        self._vision_focuses = {}
        for f in focuses:
            cid = f.get("camera_id", "")
            self._vision_focuses.setdefault(cid, []).append(f)

    def _get_combined_focus(self, camera_id: str = "") -> str:
        """拼接指定摄像头所有 enabled 项的 text,用于 classify_frame。"""
        enabled = [f["text"] for f in self._vision_focuses.get(camera_id, []) if f.get("enabled", True)]
        if not enabled:
            return "画面中的人和他们的行为"
        return "；".join(enabled)

    # ---- 向后兼容旧单条 API ----

    def set_vision_focus(self, focus: str) -> None:
        """兼容旧接口：添加一条新的 focus。"""
        text = focus.strip() if focus else "画面中的人和他们的行为"
        self.add_focus(text)

    def get_vision_focus(self) -> str:
        """兼容旧接口：返回第一条 focus 的 text；未配置时返回空字符串。

        _vision_focuses 是 {camera_id: [items]} 字典，不能用整数索引。
        取第一个 bucket 的第一项（插入顺序，py3.7+ dict 保序）。
        注意：这里只反映"用户配置"，无配置返回 ""，由推理侧兜底
        （_get_combined_focus 会用默认描述），不要在此虚构默认值。
        """
        for bucket in self._vision_focuses.values():
            if bucket:
                return bucket[0]["text"]
        return ""

    async def evaluate_condition(
        self,
        frames: list,
        condition: str,
        context_info: str = "",
        pre_encoded_b64: list[str] | None = None,
    ) -> int:
        """判断当前帧序列是否满足自然语言条件。异步，使用 httpx 连接池。

        返回 0/1：
        - 0: 条件不成立
        - 1: 条件成立

        Args:
            context_info: 可选的环境上下文（时间/天气等），透传给 VL 模型。
            pre_encoded_b64: 调用方预先编码好的 base64 帧列表，传入则跳过内部编码
                （automation_service 一个 tick 编码一次，5 条规则复用同一份）。
        """
        content = str(await self._client.evaluate_condition(
            frames, condition, context_info=context_info, pre_encoded_b64=pre_encoded_b64,
        )).strip()
        logger.info("evaluate_condition raw response: %r (condition=%r)", content[:100], condition[:30])

        # 容错解析：提取第一个出现的数字
        # 模型可能返回 "1." 或 "1\n" 或 "答案是：1" 等
        # 0 = 条件不成立；非 0 数字 = 条件成立
        import re
        match = re.search(r'\d+', content)
        if match:
            raw_num = int(match.group())
            result = 0 if raw_num == 0 else 1
            logger.info("evaluate_condition parsed: raw=%d -> %d", raw_num, result)
            return result

        logger.warning("evaluate_condition unexpected: %r", content[:50])
        return 0

    def classify_frame(self, frame_bgr, camera_id: str = "") -> ActionResult:
        if not self.enabled:
            return ActionResult("idle", "视觉模型未启用。", {"source": "vision", "enabled": False})

        import asyncio
        payload = asyncio.run(self._client.classify_frame(frame_bgr, focus=self._get_combined_focus(camera_id)))
        return self._build_result_from_payload(payload)

    async def classify_frame_async(self, frame_bgr, camera_id: str = "") -> ActionResult:
        """异步版分类，直接 await 客户端而非 asyncio.run。

        供摄像头运动推理在主事件循环里调用：httpx 网络等待时释放 GIL，
        不会像 asyncio.run + 线程池那样抢 GIL 饿死摄像头采集线程。
        camera_id 用于取该摄像头的 per-camera focus(Task 2)。
        """
        if not self.enabled:
            return ActionResult("idle", "视觉模型未启用。", {"source": "vision", "enabled": False})

        payload = await self._client.classify_frame(frame_bgr, focus=self._get_combined_focus(camera_id))
        return self._build_result_from_payload(payload)

    def _build_result_from_payload(self, payload: dict[str, Any]) -> ActionResult:
        choices = payload.get("choices") or []
        content = choices[0].get("message", {}).get("content", "") if choices else ""

        try:
            parsed = self._parse_json(content)
        except VisionInferenceException:
            logger.warning("Vision JSON parse failed, using default result, content=%r", content[:100])
            return ActionResult("no_event", "画面分析失败，请稍后重试。", {
                "source": "vision", "enabled": True, "event": "no_event",
                "observation": "", "model": self.model, "parse_error": True,
            })

        event = str(parsed.get("event", "no_event"))
        observation = str(parsed.get("observation", ""))
        details = {
            "source": "vision",
            "enabled": True,
            "event": event,
            "observation": observation,
            "model": self.model,
        }

        if event == "no_event":
            return ActionResult("no_event", feedback=observation or "画面平静，暂无事件。", details=details)
        return ActionResult(event, feedback=observation or _feedback_default(event), details=details)

    def _parse_json(self, content: str) -> dict[str, Any]:
        extracted = extract_json_from_content(content)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode vision JSON", extra={"content": content[:200]})
            raise VisionInferenceException("视觉模型返回的 JSON 无法解析") from exc
