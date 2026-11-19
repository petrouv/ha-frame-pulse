"""Entry diagnostics: entry data and options, status, counters and times.

The go2rtc address, the go2rtc API login and the RTSP login are redacted, and
the stream names are given in their safe form.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant

from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    CONF_SNAPSHOT_STREAM,
    CONF_STREAM,
)
from .coordinator import FramePulseConfigEntry
from .probe import stream_label

TO_REDACT = {
    CONF_URL,
    CONF_API_USERNAME,
    CONF_API_PASSWORD,
    CONF_RTSP_USERNAME,
    CONF_RTSP_PASSWORD,
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FramePulseConfigEntry
) -> dict[str, Any]:
    """Collect diagnostics. The frame digest is left out on purpose."""
    coordinator = entry.runtime_data
    state = coordinator.data
    options = dict(entry.options)
    if CONF_SNAPSHOT_STREAM in options:
        options[CONF_SNAPSHOT_STREAM] = stream_label(options[CONF_SNAPSHOT_STREAM])
    return {
        "entry": async_redact_data(
            {**entry.data, CONF_STREAM: coordinator.stream_label}, TO_REDACT
        ),
        "options": options,
        "status": state.status,
        "connected": state.connected,
        "failures": state.failures,
        "same_frames": state.same_frames,
        "frozen": state.frozen,
        "last_frame_at": _iso(state.last_frame_at),
        "last_check_at": _iso(state.last_check_at),
        "duration_ms": state.duration_ms,
    }
