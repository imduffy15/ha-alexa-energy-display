"""Push-now button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EnergyDisplayConfigEntry
from .entity import EnergyDisplayEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnergyDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the button."""
    async_add_entities([PushNowButton(entry.runtime_data)])


class PushNowButton(EnergyDisplayEntity, ButtonEntity):
    """Rebuild the snapshot and push it to every paired widget."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "push_now")

    async def async_press(self) -> None:
        snapshot = await self.coordinator.async_build_snapshot()
        self.coordinator.async_set_updated_data(snapshot)
        await self.coordinator.async_push(snapshot, force=True)
        self.coordinator.async_update_listeners()
