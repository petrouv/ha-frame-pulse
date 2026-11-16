"""Frame Pulse: fresh frame check through go2rtc."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.start import async_at_started

from .coordinator import FramePulseConfigEntry, FramePulseCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: FramePulseConfigEntry) -> bool:
    """Set up a config entry: one entry is one camera."""
    coordinator = FramePulseCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def first_check(_hass: HomeAssistant) -> None:
        coordinator.async_first_check()

    # While Home Assistant starts, the first check and the regular polls wait until
    # startup is over: go2rtc started by another integration may not answer yet, and
    # the camera would show probe_error until the next check. Added or reloaded
    # later, an entry is checked at once.
    entry.async_on_unload(async_at_started(hass, first_check))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FramePulseConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
