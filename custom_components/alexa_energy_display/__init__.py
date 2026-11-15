"""Alexa Energy Display: Home Assistant energy data on Echo Show screens and widgets."""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from .const import CONF_CLOUDHOOK_URL, CONF_WEBHOOK_ID, DOMAIN
from .coordinator import EnergyDisplayCoordinator
from .skill_webhook import async_handle_webhook

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

type EnergyDisplayConfigEntry = ConfigEntry[EnergyDisplayCoordinator]


async def async_webhook_url(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """Return the internet-reachable URL the skill must call.

    Prefers a Home Assistant Cloud webhook when a subscription is active, otherwise
    the configured external URL. Returns None when neither exists.
    """
    webhook_id = entry.data[CONF_WEBHOOK_ID]
    if cloudhook := entry.data.get(CONF_CLOUDHOOK_URL):
        return cloudhook
    if "cloud" in hass.config.components:
        from homeassistant.components import cloud  # noqa: PLC0415

        if cloud.async_active_subscription(hass) and cloud.async_is_connected(hass):
            try:
                url = await cloud.async_create_cloudhook(hass, webhook_id)
            except Exception:  # noqa: BLE001 - fall back to the external URL
                _LOGGER.warning("Could not create a Home Assistant Cloud webhook", exc_info=True)
            else:
                hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_CLOUDHOOK_URL: url}
                )
                return url
    try:
        return webhook.async_generate_url(
            hass, webhook_id, allow_internal=False, allow_ip=True, prefer_external=True
        )
    except NoURLAvailableError:
        return None


async def async_setup_entry(hass: HomeAssistant, entry: EnergyDisplayConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = EnergyDisplayCoordinator(hass, entry)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    webhook.async_register(
        hass,
        DOMAIN,
        entry.title,
        entry.data[CONF_WEBHOOK_ID],
        async_handle_webhook,
        local_only=False,
        allowed_methods=["POST"],
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not coordinator.users:
        url = await async_webhook_url(hass, entry)
        persistent_notification.async_create(
            hass,
            (
                "Put this URL in the skill's `lambda/config.json` as `webhook_url`, "
                "then say \"Alexa, open energy display\" once on your Echo:\n\n"
                f"`{url}`"
                if url
                else "Home Assistant has no external URL. Set one under Settings → System → "
                "Network (or sign in to Home Assistant Cloud), then reload this integration."
            ),
            title=f"Alexa Energy Display: {entry.title}",
            notification_id=f"{DOMAIN}_{entry.entry_id}",
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnergyDisplayConfigEntry) -> bool:
    """Unload a config entry."""
    webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EnergyDisplayConfigEntry) -> None:
    """Delete the cloud webhook and stored pairings."""
    if entry.data.get(CONF_CLOUDHOOK_URL) and "cloud" in hass.config.components:
        from homeassistant.components import cloud  # noqa: PLC0415

        try:
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Cloud webhook already gone", exc_info=True)
    coordinator = EnergyDisplayCoordinator(hass, entry)
    await coordinator.async_remove_store()
    persistent_notification.async_dismiss(hass, f"{DOMAIN}_{entry.entry_id}")
