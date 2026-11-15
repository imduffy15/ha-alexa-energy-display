"""Widget status sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyDisplayConfigEntry
from .entity import EnergyDisplayEntity

STATES = ["not_paired", "paired", "pull_only", "error"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the status sensor."""
    async_add_entities([WidgetStatusSensor(entry.runtime_data)])


class WidgetStatusSensor(EnergyDisplayEntity, SensorEntity):
    """Pairing and push health."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "widget_status")

    @property
    def native_value(self) -> str:
        c = self.coordinator
        if c.last_error:
            return "error"
        if not c.users:
            return "not_paired"
        return "paired" if c.widget_enabled else "pull_only"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self.coordinator
        data = c.data or {}
        return {
            "paired_accounts": len(c.users),
            "last_push": c.last_push,
            "last_push_result": c.last_push_result,
            "last_error": c.last_error,
            "tiles": len(data.get("tiles", [])),
            "snapshot_updated": data.get("updated_at"),
        }
