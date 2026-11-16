"""Sensors: status, last frame, response time."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FramePulseConfigEntry
from .entity import FramePulseEntity
from .probe import Status

# Read-only entities of a coordinator: no update limit is needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FramePulseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the camera sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            StatusSensor(coordinator),
            LastFrameSensor(coordinator),
            ResponseTimeSensor(coordinator),
        ]
    )


class StatusSensor(FramePulseEntity, SensorEntity):
    """Status of the last check; it also shows when the check itself is broken."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [status.value for status in Status]
    _attr_translation_key = "status"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.status


class LastFrameSensor(FramePulseEntity, RestoreSensor):
    """Time when the picture last changed; survives a Home Assistant restart."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "last_frame"
    _restored: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, datetime):
            self._restored = last.native_value

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.last_frame_at or self._restored


class ResponseTimeSensor(FramePulseEntity, SensorEntity):
    """How long ffmpeg ran in the last check; unknown if it did not run."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = UnitOfTime.MILLISECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _attr_translation_key = "response_time"

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.duration_ms
