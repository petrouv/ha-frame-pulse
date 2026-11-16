"""Planned shutdown switch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FramePulseConfigEntry
from .entity import FramePulseEntity

# Turning off sends no request; turning on starts a background check, limited by
# the integration semaphore.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FramePulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the camera switch."""
    async_add_entities([MonitoringSwitch(entry.runtime_data)])


class MonitoringSwitch(FramePulseEntity, SwitchEntity):
    """Camera monitoring; only the user turns it off."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "monitoring"

    @property
    def is_on(self) -> bool:
        return self.coordinator.monitoring

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_monitoring(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_monitoring(False)
