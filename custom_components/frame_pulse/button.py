"""Out-of-schedule check button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FramePulseConfigEntry
from .entity import FramePulseEntity

# One button per camera; checks are limited by the integration semaphore.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FramePulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the camera button."""
    async_add_entities([CheckNowButton(entry.runtime_data)])


class CheckNowButton(FramePulseEntity, ButtonEntity):
    """Check now."""

    _attr_translation_key = "check_now"

    async def async_press(self) -> None:
        await self.coordinator.async_check_now()
