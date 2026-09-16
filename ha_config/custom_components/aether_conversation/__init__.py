"""Aether conversation agent — 把 Aether 接成 HA Assist 的「脑子」。

入口归 HA：Assist 管线负责 wake word / STT / TTS，本 agent 只把识别出的
文本转发给 Aether 的单轮对话 API（POST {host}/api/assist/chat，X-API-Token
鉴权），拿到回复交给管线播报。Aether 不可达时回一句固定话术（不硬失败，
语音链路仍有响应），复杂请求的理解与设备控制在 Aether 侧完成。

组件目录随 Aether 仓库分发（aether-ha 容器挂载 ha_config），更新后重启
HA 生效；在 HA「设置 → 设备与服务 → 添加集成 → Aether」填 Aether 地址
与 API Token，再到「语音助手」设置里把对话引擎选成 Aether 即可。
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant import intent
import uuid

from .const import CONF_HOST, CONF_TOKEN, DEFAULT_TIMEOUT_SECONDS, DOMAIN

_LOGGER = logging.getLogger(__name__)

_FALLBACK_REPLY = "Aether 暂时连不上，请稍后再试。"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """注册 conversation agent（单实例）。"""
    session = async_get_clientsession(hass)
    conversation.async_set_agent(hass, entry, AetherAgent(hass, entry, session))
    # AbstractConversationAgent 需挂一个设备条目（语音助手页可见）
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Aether 助手",
        manufacturer="Aether",
        model="Conversation Agent",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    conversation.async_unset_agent(hass, entry)
    return True


class AetherAgent(conversation.AbstractConversationAgent):
    """转发文本给 Aether 的 conversation agent。"""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, session: aiohttp.ClientSession
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._session = session

    @property
    def supported_languages(self) -> list[str]:
        # "*" 通配：语言判断/播报都在 Aether 与 Assist 管线两侧各自处理
        return ["*"]

    async def async_handle(
        self,
        hass: HomeAssistant,
        text: str,
        context: conversation.ConversationContext,
        language: str,
        conversation_id: str | None,
        device_id: str | None,
    ) -> conversation.ConversationResult:
        host = str(self._entry.data.get(CONF_HOST, "")).rstrip("/")
        token = str(self._entry.data.get(CONF_TOKEN, ""))
        reply = ""
        cid = conversation_id or uuid.uuid4().hex
        if host and token:
            try:
                resp = await self._session.post(
                    f"{host}/api/assist/chat",
                    json={"text": text, "conversation_id": cid},
                    headers={"X-API-Token": token},
                    timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS),
                )
                if resp.status == 200:
                    data: dict[str, Any] = await resp.json()
                    # ApiResponse 信封：{code, message, data:{reply,...}}
                    payload = data.get("data") or {}
                    reply = str(payload.get("reply", "") or "")
                else:
                    _LOGGER.warning(
                        "Aether chat HTTP %s: %s", resp.status, await resp.text()
                    )
            except (TimeoutError, aiohttp.ClientError):
                _LOGGER.warning("Aether unreachable", exc_info=True)
        if not reply:
            reply = _FALLBACK_REPLY

        intent_response = intent.IntentResponse(language=language)
        intent_response.async_set_speech(reply)
        return conversation.ConversationResult(
            conversation_id=cid, response=intent_response
        )
