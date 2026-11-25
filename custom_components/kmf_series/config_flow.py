"""Config flow for the KM-F Series integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)

from .client import DEFAULT_PORT, KmfcClient, KmfcConnectionError
from .const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    DEFAULT_NAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

HOST_PATTERN = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"
    r"|^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$"
)

STEP_USER = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): TextSelector(),
        vol.Required(CONF_HOST): TextSelector(),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(
                min=1, max=65535, mode=NumberSelectorMode.BOX, step=1
            )
        ),
    }
)


def valid_host(value: str) -> bool:
    """Accept a dotted quad or a plain hostname."""
    return bool(HOST_PATTERN.match(value.strip()))


class KmfcConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup of a KM-F device."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "KmfcOptionsFlow":
        """Return the options handler for an existing entry."""
        return KmfcOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the endpoint, then reach out to the device before saving."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if not valid_host(host):
                errors[CONF_HOST] = "invalid_host"
            else:
                await self.async_set_unique_id(host.lower())
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: host,
                        CONF_PORT: user_input[CONF_PORT],
                    }
                )

                client = KmfcClient(host, int(user_input[CONF_PORT]))
                try:
                    await client.probe()
                except KmfcConnectionError as err:
                    _LOGGER.debug("KM-F probe failed for %s: %s", host, err)
                    errors["cannot_connect"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME) or DEFAULT_NAME,
                        data={
                            CONF_NAME: user_input.get(CONF_NAME)
                            or DEFAULT_NAME,
                            CONF_HOST: host,
                            CONF_PORT: int(user_input[CONF_PORT]),
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER, errors=errors
        )


class KmfcOptionsFlow(OptionsFlow):
    """Handle the options of an existing KM-F entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user retune the polling cadence."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Optional(UPDATE_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_UPDATE_INTERVAL,
                        max=MAX_UPDATE_INTERVAL,
                        mode=NumberSelectorMode.BOX,
                        step=1,
                        unit_of_measurement="s",
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
