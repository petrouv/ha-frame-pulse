"""Binary sensors: connection and frozen picture."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity

from .coordinator import FramePulseConfigEntry
from .entity import FramePulseEntity
from .probe import Status

# The check did not run (monitoring off) or is broken, so the sensors are unavailable,
# not an alarm. With `stream_not_found` they stay available and keep their last value.
_UNKNOWN_CAMERA = (Status.PROBE_ERROR, Status.DISABLED)

# Read-only entities of a coordinator: no update limit is needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FramePulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the camera sensors."""
    coordinator = entry.runtime_data
    async_add_entities([ConnectivitySensor(coordinator), FrozenSensor(coordinator)])


class _CameraBinarySensor(FramePulseEntity, BinarySensorEntity, RestoreEntity):
    """Binary sensor that is unavailable when nothing is known about the camera.

    It keeps its part of the camera state across a restart and a reload of the
    entry, so the first check after them continues where the last one stopped: a
    camera that came back meanwhile turns the connection from `off` to `on`, which
    an automation can catch, instead of from `unknown`.
    """

    # Fields of the camera state this sensor keeps, with their allowed types.
    _kept: ClassVar[dict[str, tuple[type, ...]]]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_extra_data()) is None:
            return
        saved = last.as_dict()
        # Exact types: a saved value of another type (`True` for a counter) is not trusted.
        if all(type(saved.get(name)) in types for name, types in self._kept.items()):
            self.coordinator.async_restore({name: saved[name] for name in self._kept})

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        data = self.coordinator.data
        return RestoredExtraData({name: getattr(data, name) for name in self._kept})

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.status not in _UNKNOWN_CAMERA


class ConnectivitySensor(_CameraBinarySensor):
    """The camera returns frames, even frozen ones.

    Off only after the set number of failed checks in a row.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"
    _kept: ClassVar[dict[str, tuple[type, ...]]] = {
        "connected": (bool, type(None)),
        "failures": (int,),
    }

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.connected


class FrozenSensor(_CameraBinarySensor):
    """The picture is frozen; a failed check in between does not turn it off."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "frozen"
    # The frame digest is kept too: without it the first frame after a restart would
    # count as new and a frozen picture would turn `off`, then `on` again.
    _kept: ClassVar[dict[str, tuple[type, ...]]] = {
        "frozen": (bool,),
        "same_frames": (int,),
        "last_digest": (str, type(None)),
    }

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        # Unknown until a frame has been received: before it nothing is known about the picture.
        return None if data.last_digest is None else data.frozen
