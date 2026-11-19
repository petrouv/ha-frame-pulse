"""Frame Pulse: fresh frame check through go2rtc."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .config_flow import FramePulseConfigFlow
from .const import CONF_MONITORING, DOMAIN
from .coordinator import FramePulseConfigEntry, FramePulseCoordinator
from .snapshot import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once for the integration, so they exist with no camera loaded."""
    async_setup_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: FramePulseConfigEntry) -> bool:
    """Migrate an entry saved by an older version before it is set up.

    Minor version 2 (0.1.1) keeps the monitoring flag in the options instead of the
    data. A minor version change stays backwards compatible: 0.1.0 still loads a
    migrated entry, only with monitoring on. An entry of a newer major version is
    refused, since this code cannot know its format.
    """
    if entry.version > FramePulseConfigFlow.VERSION:
        return False
    if entry.minor_version < 2:
        if CONF_MONITORING in entry.data:
            data = dict(entry.data)
            options = {**entry.options, CONF_MONITORING: data.pop(CONF_MONITORING)}
            hass.config_entries.async_update_entry(
                entry, data=data, options=options, minor_version=2
            )
        else:
            hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


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
