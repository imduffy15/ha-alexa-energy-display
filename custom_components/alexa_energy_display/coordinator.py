"""Snapshot refresh, Alexa user pairing and widget push."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    ALLOWED_API_ENDPOINTS,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENTITIES,
    CONF_ENTITIES_FIRST,
    CONF_TITLE,
    CONF_UPDATE_INTERVAL,
    CONF_USE_ENERGY,
    DEFAULT_API_ENDPOINT,
    DEFAULT_TITLE,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_PUSH_AGE_SECONDS,
    STORAGE_VERSION,
)
from .datastore import AuthError, DataStoreClient, DataStoreError
from .snapshot import (
    EnergySources,
    assemble,
    async_energy_tiles,
    content_key,
    entity_tiles,
    parse_energy_prefs,
)

_LOGGER = logging.getLogger(__name__)


async def async_get_energy_sources(hass: HomeAssistant) -> EnergySources:
    """Read the Energy dashboard configuration, if the energy integration is loaded."""
    if "energy" not in hass.config.components:
        return EnergySources()
    from homeassistant.components.energy.data import async_get_manager  # noqa: PLC0415

    manager = await async_get_manager(hass)
    return parse_energy_prefs(manager.data)


class EnergyDisplayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Build the snapshot on an interval and push it to paired Alexa users."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        options = entry.options
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=int(options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
            ),
            always_update=True,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.users: dict[str, dict[str, Any]] = {}
        self.client: DataStoreClient | None = None
        if options.get(CONF_CLIENT_ID) and options.get(CONF_CLIENT_SECRET):
            self.client = DataStoreClient(
                async_get_clientsession(hass),
                options[CONF_CLIENT_ID],
                options[CONF_CLIENT_SECRET],
            )
        self.last_push: str | None = None
        self.last_push_result: str | None = None
        self.last_error: str | None = None
        self._last_push_key: tuple | None = None
        self._last_push_monotonic = 0.0
        self._push_lock = asyncio.Lock()
        self.built_monotonic = 0.0
        self.metrics: dict[str, float] = {}
        self.energy_sources = EnergySources()

    @property
    def widget_enabled(self) -> bool:
        """Return True when skill messaging credentials are configured."""
        return self.client is not None

    async def async_load(self) -> None:
        """Load paired Alexa users from storage."""
        data = await self._store.async_load() or {}
        self.users = dict(data.get("users", {}))

    async def _async_save(self) -> None:
        await self._store.async_save({"users": self.users})

    async def _async_update_data(self) -> dict[str, Any]:
        snapshot = await self.async_build_snapshot()
        if self.widget_enabled and self.users:
            await self.async_push(snapshot)
        return snapshot

    async def async_build_snapshot(self) -> dict[str, Any]:
        """Collect tiles from the Energy dashboard and the chosen entities."""
        options = self.config_entry.options
        tiles: list[dict[str, Any]] = []
        metrics: dict[str, float] = {}
        sources = EnergySources()
        if options.get(CONF_USE_ENERGY, True):
            sources = await async_get_energy_sources(self.hass)
            tiles.extend(await async_energy_tiles(self.hass, sources, metrics))
        self.energy_sources = sources
        self.metrics = metrics
        custom = entity_tiles(self.hass, options.get(CONF_ENTITIES, []))
        tiles = custom + tiles if options.get(CONF_ENTITIES_FIRST, False) else tiles + custom
        self.built_monotonic = time.monotonic()
        return assemble(options.get(CONF_TITLE) or DEFAULT_TITLE, tiles)

    async def async_push(self, snapshot: dict[str, Any], force: bool = False) -> None:
        """Push when content changed, when the last push is old, or when forced."""
        if self.client is None or not self.users:
            return
        key = content_key(snapshot)
        age = time.monotonic() - self._last_push_monotonic
        if not force and key == self._last_push_key and age < MAX_PUSH_AGE_SECONDS:
            return
        async with self._push_lock:
            results = []
            errors = []
            for user_id, info in list(self.users.items()):
                endpoint = info.get("api_endpoint") or DEFAULT_API_ENDPOINT
                try:
                    result = await self.client.async_put_snapshot(endpoint, user_id, snapshot)
                except AuthError as err:
                    errors.append(f"credentials rejected: {err}")
                    break
                except DataStoreError as err:
                    errors.append(str(err))
                    continue
                results.append(result.detail)
            self.last_push = dt_util.utcnow().isoformat(timespec="seconds")
            if errors:
                self.last_error = "; ".join(errors)
                self.last_push_result = "error"
                _LOGGER.warning("Widget push failed: %s", self.last_error)
            else:
                self.last_error = None
                self.last_push_result = "; ".join(results) or "ok"
                self._last_push_key = key
                self._last_push_monotonic = time.monotonic()
        self.async_update_listeners()

    async def async_register_user(
        self, user_id: str, device_id: str | None, api_endpoint: str | None
    ) -> bool:
        """Remember an Alexa user (and device) that contacted the skill. Return True if new."""
        if api_endpoint not in ALLOWED_API_ENDPOINTS:
            api_endpoint = DEFAULT_API_ENDPOINT
        info = self.users.get(user_id)
        is_new = info is None
        info = info or {"devices": []}
        changed = is_new or info.get("api_endpoint") != api_endpoint
        info["api_endpoint"] = api_endpoint
        if device_id and device_id not in info["devices"]:
            info["devices"] = [*info["devices"], device_id][-20:]
            changed = True
        info["last_seen"] = dt_util.utcnow().isoformat(timespec="seconds")
        self.users[user_id] = info
        if changed:
            await self._async_save()
        return is_new

    async def async_forget_users(self) -> None:
        """Drop every paired user."""
        self.users = {}
        await self._async_save()
        self.async_update_listeners()

    async def async_remove_store(self) -> None:
        """Delete persisted pairings when the entry is removed."""
        await self._store.async_remove()
