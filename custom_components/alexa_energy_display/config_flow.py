"""Config and options flow."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DASHBOARD_URL,
    CONF_ENTITIES,
    CONF_ENTITIES_FIRST,
    CONF_SH_CLIENT_ID,
    CONF_SH_CLIENT_SECRET,
    CONF_SKILL_ID,
    CONF_TITLE,
    CONF_UPDATE_INTERVAL,
    CONF_USE_ENERGY,
    CONF_VOICE_PREFIX,
    CONF_WEBHOOK_ID,
    DEFAULT_TITLE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .datastore import AuthError, DataStoreClient, DataStoreError

CONF_FORGET = "forget_accounts"


def _credentials_schema(defaults: dict[str, Any]) -> dict:
    return {
        vol.Optional(CONF_CLIENT_ID, description={"suggested_value": defaults.get(CONF_CLIENT_ID)}): TextSelector(),
        vol.Optional(
            CONF_CLIENT_SECRET, description={"suggested_value": defaults.get(CONF_CLIENT_SECRET)}
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    }


def _sources_schema(defaults: dict[str, Any]) -> dict:
    return {
        vol.Required(CONF_USE_ENERGY, default=defaults.get(CONF_USE_ENERGY, True)): BooleanSelector(),
        vol.Optional(CONF_ENTITIES, default=defaults.get(CONF_ENTITIES, [])): EntitySelector(
            EntitySelectorConfig(multiple=True)
        ),
        vol.Required(
            CONF_ENTITIES_FIRST, default=defaults.get(CONF_ENTITIES_FIRST, False)
        ): BooleanSelector(),
        vol.Optional(
            CONF_DASHBOARD_URL, description={"suggested_value": defaults.get(CONF_DASHBOARD_URL)}
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        vol.Required(
            CONF_UPDATE_INTERVAL, default=defaults.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        ): NumberSelector(
            NumberSelectorConfig(
                min=MIN_UPDATE_INTERVAL,
                max=MAX_UPDATE_INTERVAL,
                step=10,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(CONF_SKILL_ID, description={"suggested_value": defaults.get(CONF_SKILL_ID)}): TextSelector(),
    }


async def _validate_credentials(hass, data: dict[str, Any]) -> dict[str, str]:
    client_id = (data.get(CONF_CLIENT_ID) or "").strip()
    client_secret = (data.get(CONF_CLIENT_SECRET) or "").strip()
    if not client_id and not client_secret:
        return {}
    if not client_id or not client_secret:
        return {"base": "incomplete_credentials"}
    client = DataStoreClient(async_get_clientsession(hass), client_id, client_secret)
    try:
        await client.async_get_token(force=True)
    except AuthError:
        return {"base": "invalid_auth"}
    except DataStoreError:
        return {"base": "cannot_connect"}
    return {}


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}
    out[CONF_UPDATE_INTERVAL] = int(out.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    return {k: v for k, v in out.items() if v not in ("", None)}


class EnergyDisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two steps: name and skill credentials, then data sources."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate_credentials(self.hass, user_input)
            if not errors:
                self._data = user_input
                return await self.async_step_sources()
        schema = vol.Schema(
            {
                vol.Required(CONF_TITLE, default=(user_input or {}).get(CONF_TITLE, DEFAULT_TITLE)): TextSelector(),
                **_credentials_schema(user_input or {}),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sources(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_USE_ENERGY) and not user_input.get(CONF_ENTITIES):
                errors["base"] = "no_sources"
            else:
                options = _clean({**self._data, **user_input})
                title = options.pop(CONF_TITLE, DEFAULT_TITLE)
                options[CONF_TITLE] = title
                return self.async_create_entry(
                    title=title,
                    data={CONF_WEBHOOK_ID: secrets.token_hex(32)},
                    options=options,
                )
        return self.async_show_form(
            step_id="sources",
            data_schema=vol.Schema(_sources_schema(user_input or {})),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EnergyDisplayOptionsFlow:
        return EnergyDisplayOptionsFlow()


class EnergyDisplayOptionsFlow(OptionsFlowWithReload):
    """Change any setting; also shows the webhook URL for the skill."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        from . import async_webhook_url  # noqa: PLC0415

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_USE_ENERGY) and not user_input.get(CONF_ENTITIES):
                errors["base"] = "no_sources"
            else:
                errors = await _validate_credentials(self.hass, user_input)
            if not errors:
                if user_input.pop(CONF_FORGET, False):
                    await self.config_entry.runtime_data.async_forget_users()
                return self.async_create_entry(data=_clean(user_input))
        current = {**self.config_entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(CONF_TITLE, default=current.get(CONF_TITLE, DEFAULT_TITLE)): TextSelector(),
                **_credentials_schema(current),
                **_sources_schema(current),
                vol.Optional(
                    CONF_VOICE_PREFIX, description={"suggested_value": current.get(CONF_VOICE_PREFIX)}
                ): TextSelector(),
                vol.Optional(
                    CONF_SH_CLIENT_ID, description={"suggested_value": current.get(CONF_SH_CLIENT_ID)}
                ): TextSelector(),
                vol.Optional(
                    CONF_SH_CLIENT_SECRET, description={"suggested_value": current.get(CONF_SH_CLIENT_SECRET)}
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(CONF_FORGET, default=False): BooleanSelector(),
            }
        )
        url = await async_webhook_url(self.hass, self.config_entry)
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"webhook_url": url or "(no external URL configured)"},
        )
