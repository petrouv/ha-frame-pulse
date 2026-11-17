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
    def available(self) -> bool:
        """Stay available while Home Assistant asks for the go2rtc API login.

        The flag needs no check, so a planned shutdown can be marked meanwhile.
        Turning on then runs one check: a refused login keeps polling stopped,
        and Home Assistant does not start a second login request while the first
        one is open (a dismissed one is started again). Other failures leave the
        switch unavailable like the other entities.
        """
        return super().available or self.coordinator.auth_failed

    @property
    def is_on(self) -> bool:
        return self.coordinator.monitoring

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_set_monitoring(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_set_monitoring(False)
