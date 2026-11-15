"""Webhook called by the Alexa skill.

Request (JSON POST from the skill's Lambda):
    {"type": "launch" | "refresh" | "open_dashboard" | "widget_installed"
             | "widget_removed" | "datastore_error",
     "user_id": "amzn1.ask.account...", "device_id": "amzn1.ask.device...",
     "api_endpoint": "https://api.eu.amazonalexa.com", "skill_id": "amzn1.ask.skill...",
     "detail": {...}}

Response:
    {"snapshot": {...}, "dashboard_url": "https://..." | null, "widget": true | false}
"""

from __future__ import annotations

import json
import logging
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .const import CONF_DASHBOARD_URL, CONF_SKILL_ID, DOMAIN

if TYPE_CHECKING:
    from .coordinator import EnergyDisplayCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = frozenset(
    {"launch", "refresh", "open_dashboard", "widget_installed", "widget_removed", "datastore_error"}
)
# A pull younger than this reuses the last snapshot instead of rebuilding it.
_FRESH_SECONDS = 10
_MAX_BODY = 64 * 1024


def _error(status: HTTPStatus, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def async_handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    """Pair the caller, then return the current snapshot."""
    coordinator: EnergyDisplayCoordinator | None = None
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.data.get("webhook_id") == webhook_id:
            coordinator = entry.runtime_data
            break
    if coordinator is None:
        return _error(HTTPStatus.NOT_FOUND, "not configured")

    if request.content_length and request.content_length > _MAX_BODY:
        return _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
    try:
        body: dict[str, Any] = json.loads(await request.text() or "{}")
    except ValueError:
        return _error(HTTPStatus.BAD_REQUEST, "invalid JSON")
    if not isinstance(body, dict):
        return _error(HTTPStatus.BAD_REQUEST, "invalid JSON")

    event = body.get("type", "launch")
    if event not in EVENT_TYPES:
        return _error(HTTPStatus.BAD_REQUEST, "unknown type")

    options = coordinator.config_entry.options
    required_skill = (options.get(CONF_SKILL_ID) or "").strip()
    if required_skill and body.get("skill_id") != required_skill:
        _LOGGER.warning("Rejected webhook call from skill %s", body.get("skill_id"))
        return _error(HTTPStatus.FORBIDDEN, "skill not allowed")

    user_id = body.get("user_id")
    is_new = False
    if isinstance(user_id, str) and user_id.startswith("amzn1.") and len(user_id) < 512:
        device_id = body.get("device_id")
        if not (isinstance(device_id, str) and device_id.startswith("amzn1.") and len(device_id) < 512):
            device_id = None
        api_endpoint = body.get("api_endpoint")
        is_new = await coordinator.async_register_user(
            user_id, device_id, api_endpoint if isinstance(api_endpoint, str) else None
        )
        if is_new:
            _LOGGER.info("Paired a new Alexa account with %s", coordinator.config_entry.title)
            coordinator.async_update_listeners()

    if event == "datastore_error":
        coordinator.last_error = f"Alexa reported: {str(body.get('detail'))[:300]}"
        coordinator.async_update_listeners()

    snapshot = coordinator.data
    stale = snapshot is None or (
        event in ("launch", "refresh")
        and time.monotonic() - coordinator.built_monotonic > _FRESH_SECONDS
    )
    if stale:
        snapshot = await coordinator.async_build_snapshot()
        coordinator.async_set_updated_data(snapshot)

    if is_new or event == "widget_installed":
        hass.async_create_background_task(
            coordinator.async_push(snapshot, force=True), f"{DOMAIN}_initial_push"
        )

    return web.json_response(
        {
            "snapshot": snapshot,
            "dashboard_url": options.get(CONF_DASHBOARD_URL) or None,
            "widget": coordinator.widget_enabled,
        }
    )
