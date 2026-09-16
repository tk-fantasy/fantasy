"""Aether conversation agent 配置流 — 填 Aether 地址与 API Token。"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow

from .const import CONF_HOST, CONF_TOKEN, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_TOKEN): str,
    }
)


class AetherConfigFlow(ConfigFlow, domain=DOMAIN):
    """单实例配置流。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input.get(CONF_HOST, "")).strip().rstrip("/")
            if not host.startswith(("http://", "https://")):
                errors[CONF_HOST] = "invalid_host"
            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Aether",
                    data={CONF_HOST: host, CONF_TOKEN: user_input[CONF_TOKEN]},
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry,
    ) -> AetherOptionsFlow:
        return AetherOptionsFlow()


class AetherOptionsFlow(OptionsFlow):
    """修改 Aether 地址/Token（不删条目重配）。"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        if user_input is not None:
            host = str(user_input.get(CONF_HOST, "")).strip().rstrip("/")
            if not host.startswith(("http://", "https://")):
                errors[CONF_HOST] = "invalid_host"
            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_HOST: host,
                          CONF_TOKEN: user_input[CONF_TOKEN]},
                )
                return self.async_create_entry(title="", data={})
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST, "")): str,
                vol.Required(CONF_TOKEN, default=entry.data.get(CONF_TOKEN, "")): str,
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
