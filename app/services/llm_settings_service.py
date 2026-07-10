from __future__ import annotations

import logging
from typing import Any

from ..core.config import get_config, update_config_section
from ..core.exceptions import AppException
from ..core.key_resolver import find_key_by_id, resolve_api_key

logger = logging.getLogger(__name__)

ROLE_KEYS = ["chat", "summary", "vision", "embed", "stt"]


class LlmSettingsService:
    """管理各角色（chat/summary/vision/embed）的运行时配置：
    key_id、max_concurrency、thinking、multimodal。
    保存到 config.json 的 providers.<role>，并热重载客户端。
    """

    def __init__(self) -> None:
        self._reload_hooks: list[Any] = []

    def register_reload_hook(self, fn) -> None:
        """注册一个无参回调，保存设置后调用以热重载对应客户端。"""
        self._reload_hooks.append(fn)

    def _run_hooks(self) -> None:
        for fn in self._reload_hooks:
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.exception("LLM settings reload hook failed")

    def current_settings(self) -> dict[str, Any]:
        """返回每个角色当前配置：key_id, max_concurrency, thinking, multimodal。"""
        result: dict[str, Any] = {}
        for role in ROLE_KEYS:
            result[role] = {
                "key_id": get_config(f"providers.{role}.key_id"),
                "max_concurrency": int(get_config(f"providers.{role}.max_concurrency", 8)),
                "thinking": bool(get_config(f"providers.{role}.thinking", False)),
                "multimodal": bool(get_config(f"providers.{role}.multimodal", False)),
            }
        return result

    def apply(
        self,
        role: str,
        key_id: str,
        max_concurrency: int = 8,
        thinking: bool | None = None,
        multimodal: bool | None = None,
    ) -> dict[str, Any]:
        """保存角色配置到 providers.<role>。"""
        if role not in ROLE_KEYS:
            raise AppException(f"未知角色: {role}", code="llm_settings_error", http_status=400)

        values: dict[str, Any] = {
            "key_id": key_id,
            "max_concurrency": max(1, max_concurrency),
            "enabled": True,
        }

        # thinking 仅对 chat/summary/vision 有效
        if role in ("chat", "summary", "vision"):
            values["thinking"] = bool(thinking) if thinking is not None else False

        # multimodal 仅对 vision 有效
        if role == "vision":
            values["multimodal"] = bool(multimodal) if multimodal is not None else True

        section = update_config_section("providers", {role: values})
        self._run_hooks()
        logger.info("LLM settings applied", extra={"role": role, "key_id": key_id})
        return {"role": role, "applied": values, "providers": section.get(role, {})}

    def warnings(self) -> list[str]:
        """配置体检：选了 key 但对应 api_key 没设，给前端提示。"""
        notes: list[str] = []

        for role in ROLE_KEYS:
            key_id = get_config(f"providers.{role}.key_id")
            if not key_id:
                notes.append(f"{role} 未选择 key")
                continue

            key_entry = find_key_by_id(key_id)
            if not key_entry:
                notes.append(f"{role} 选择的 key ({key_id}) 不存在")
                continue

            api_key = resolve_api_key(key_entry)
            if not api_key:
                env_name = key_entry.get("api_key_env", "")
                if env_name:
                    notes.append(f"{role} 的 key 需要环境变量 {env_name}，但未设置")
                else:
                    notes.append(f"{role} 的 key 未设置 API key")

        return notes
