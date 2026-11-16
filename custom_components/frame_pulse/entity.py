"""Base entity class."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FramePulseCoordinator


class FramePulseEntity(CoordinatorEntity[FramePulseCoordinator]):
    """Binds an entity to the coordinator and the camera device."""

    _attr_has_entity_name = True
    # Each entity class sets its key; it also makes the unique ID.
    _attr_translation_key: str

    def __init__(self, coordinator: FramePulseCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_translation_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            model=coordinator.stream_label,
        )
