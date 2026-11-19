"""The `frame_pulse.snapshot` action: save one frame of a camera as a JPEG file.

The frame is taken the way a check takes it (see `probe.probe_frame`), from the
snapshot stream of the entry, and written only when it is a valid JPEG. The file
appears whole or not at all: the frame goes to a hidden temporary file in the
same folder first and is renamed over the target.
"""

from __future__ import annotations

import os
import secrets
import time
from contextlib import suppress
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.template import Template

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DEVICE_ID,
    ATTR_FILENAME,
    ATTR_TIMEOUT,
    DOMAIN,
    MAX_FRAME_TIMEOUT,
    MIN_FRAME_TIMEOUT,
    SERVICE_SNAPSHOT,
)
from .coordinator import FramePulseConfigEntry
from .probe import Status

TEMPORARY_PREFIX = ".frame_pulse-"
TEMPORARY_SUFFIX = ".tmp"
# A temporary file lives for the few milliseconds of one write. One this old was left by
# a write cut short (Home Assistant killed or the machine powered off) and is removed.
STALE_TEMPORARY_SECONDS = 60

SNAPSHOT_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Exclusive(ATTR_CONFIG_ENTRY_ID, "camera"): cv.string,
            vol.Exclusive(ATTR_DEVICE_ID, "camera"): cv.string,
            vol.Required(ATTR_FILENAME): cv.template,
            vol.Optional(ATTR_TIMEOUT): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_FRAME_TIMEOUT, max=MAX_FRAME_TIMEOUT)
            ),
        }
    ),
    cv.has_at_least_one_key(ATTR_CONFIG_ENTRY_ID, ATTR_DEVICE_ID),
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the integration actions."""

    async def snapshot(call: ServiceCall) -> dict[str, Any]:
        return await _async_snapshot(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SNAPSHOT,
        snapshot,
        schema=SNAPSHOT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _entry(hass: HomeAssistant, call: ServiceCall) -> FramePulseConfigEntry:
    """Return the loaded camera entry the call names by entry or by device.

    Raises:
        ServiceValidationError: No Frame Pulse camera has this entry or device,
            or its entry is not loaded.
    """
    if ATTR_CONFIG_ENTRY_ID in call.data:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
    else:
        # A device belongs to a single entry: a camera device to its camera entry.
        device = dr.async_get(hass).async_get(call.data[ATTR_DEVICE_ID])
        entry_id = device.config_entry_id if device else None
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="camera_not_found")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="camera_not_loaded",
            translation_placeholders={"camera": entry.title},
        )
    return entry


def _remove_stale_temporary(folder: str) -> None:
    """Remove temporary files that a write cut short left in the folder.

    Hidden files are not shown in Media, but a cleanup that deletes old `*.jpg`
    files would never remove them.
    """
    limit = time.time() - STALE_TEMPORARY_SECONDS
    with os.scandir(folder) as items:
        for item in items:
            if item.name.startswith(TEMPORARY_PREFIX) and item.name.endswith(TEMPORARY_SUFFIX):
                with suppress(OSError):
                    if item.stat(follow_symlinks=False).st_mtime < limit:
                        os.unlink(item.path)


def _write_atomic(path: str, frame: bytes) -> None:
    """Write the frame to `path` whole, through a temporary file in the same folder.

    The temporary file is hidden (Media does not list it) and created with the
    usual permissions, so the result is readable the way a file written by
    `camera.snapshot` is. It is removed when the write or the rename fails; one
    left by a write cut short is removed by a later snapshot to the same folder.
    """
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    _remove_stale_temporary(folder)
    # A short name of its own: one built from the target could pass the name length limit.
    temporary = os.path.join(folder, f"{TEMPORARY_PREFIX}{secrets.token_hex(8)}{TEMPORARY_SUFFIX}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(frame)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _path_problem(hass: HomeAssistant, path: str) -> str | None:
    """Return the error key of a target path that cannot be used, `None` if it can."""
    if not hass.config.is_allowed_path(path):
        return "path_not_allowed"
    if os.path.isdir(path):
        return "path_is_folder"
    return None


async def _async_snapshot(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    """Take a frame and save it; return the path and the size of the file.

    Raises:
        ServiceValidationError: The camera is unknown or not loaded, or the path
            is not absolute, not allowed or a folder.
        HomeAssistantError: No valid frame, or the file could not be written.
    """
    entry = _entry(hass, call)
    # Taken before the first wait: an entry unloaded meanwhile (options saved, Home
    # Assistant stopping) loses its runtime data, and reading it then would raise an
    # error that `continue_on_error` does not catch. The coordinator keeps working.
    coordinator = entry.runtime_data
    filename: Template = call.data[ATTR_FILENAME]
    path = filename.async_render(parse_result=False)
    if not os.path.isabs(path):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="path_not_absolute",
            translation_placeholders={"path": path},
        )
    # A folder is refused before any frame is taken, also one named with a trailing
    # slash, which normpath would turn into the folder itself.
    if path.endswith(os.sep) or os.path.basename(path) in (".", ".."):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="path_is_folder",
            translation_placeholders={"path": path},
        )
    path = os.path.normpath(path)
    if problem := await hass.async_add_executor_job(_path_problem, hass, path):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=problem,
            translation_placeholders={"path": path},
        )

    result = await coordinator.async_grab_snapshot(call.data.get(ATTR_TIMEOUT))
    if result.auth_failed:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="api_auth_failed")
    if result.status is not Status.OK:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="snapshot_failed",
            translation_placeholders={
                "camera": entry.title,
                "status": result.status,
                "detail": result.detail,
            },
        )

    try:
        await hass.async_add_executor_job(_write_atomic, path, result.frame)
    except OSError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="snapshot_write_failed",
            translation_placeholders={
                "path": path,
                "error": err.strerror or type(err).__name__,
            },
        ) from err
    return {"path": path, "size": len(result.frame)}
