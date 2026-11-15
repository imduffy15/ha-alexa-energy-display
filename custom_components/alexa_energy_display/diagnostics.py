"""Diagnostics with secrets redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EnergyDisplayConfigEntry
from .const import CONF_CLIENT_SECRET, CONF_CLOUDHOOK_URL, CONF_WEBHOOK_ID

TO_REDACT = {
    CONF_CLIENT_SECRET,
    CONF_WEBHOOK_ID,
    CONF_CLOUDHOOK_URL,
    "client_id",
    "smart_home_client_id",
    "smart_home_client_secret",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnergyDisplayConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    c = entry.runtime_data
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "paired_accounts": len(c.users),
        "endpoints": sorted({u.get("api_endpoint") for u in c.users.values()}),
        "last_push": c.last_push,
        "last_push_result": c.last_push_result,
        "last_error": c.last_error,
        "snapshot": c.data,
    }
