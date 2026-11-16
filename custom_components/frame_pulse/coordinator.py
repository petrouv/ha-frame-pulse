"""Check loop of a single camera."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey
from yarl import URL

from .const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_FAILURES,
    CONF_FRAME_TIMEOUT,
    CONF_MONITORING,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_SAME_FRAMES,
    CONF_STREAM,
    DEFAULT_FAILURES,
    DEFAULT_FRAME_TIMEOUT,
    DEFAULT_SAME_FRAMES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CONCURRENT_PROBES,
)
from .probe import (
    ApiAccess,
    CameraState,
    Limits,
    RtspAccess,
    apply_result,
    disabled_state,
    probe_frame,
    stream_label,
)

_LOGGER = logging.getLogger(__name__)

_SEMAPHORE: HassKey[asyncio.Semaphore] = HassKey(DOMAIN)

type FramePulseConfigEntry = ConfigEntry[FramePulseCoordinator]


def entry_unique_id(url: str, stream: str) -> str:
    """Return the unique ID of a camera: the go2rtc instance plus the stream.

    The same stream name on another go2rtc is another camera. Credentials from
    the address are left out. A stream name that may carry a secret (see
    `stream_label`) is replaced by its SHA-256, a plain name stays readable.
    The path is kept encoded, so `/a%2Fb` and `/a/b` stay apart.
    """
    parsed = URL(url)
    if stream_label(stream) != stream:
        stream = f"sha256:{hashlib.sha256(stream.encode()).hexdigest()}"
    return f"{parsed.host}:{parsed.port}{parsed.raw_path.rstrip('/')}|{stream}"


def rtsp_access(data: Mapping[str, Any]) -> RtspAccess:
    """Return the go2rtc RTSP server settings of an entry."""
    return RtspAccess(
        port=data.get(CONF_RTSP_PORT),
        username=data.get(CONF_RTSP_USERNAME, ""),
        password=data.get(CONF_RTSP_PASSWORD, ""),
    )


def api_access(data: Mapping[str, Any]) -> ApiAccess:
    """Return the go2rtc API login of an entry; an entry without one has no keys."""
    return ApiAccess(
        username=data.get(CONF_API_USERNAME, ""),
        password=data.get(CONF_API_PASSWORD, ""),
    )


def probe_semaphore(hass: HomeAssistant) -> asyncio.Semaphore:
    """Return the check semaphore shared by the whole integration."""
    return hass.data.setdefault(_SEMAPHORE, asyncio.Semaphore(MAX_CONCURRENT_PROBES))


class FramePulseCoordinator(DataUpdateCoordinator[CameraState]):
    """Polls go2rtc; a camera failure is data here, not `UpdateFailed`."""

    config_entry: FramePulseConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FramePulseConfigEntry) -> None:
        options = entry.options
        # No schedule until the first check: the first entity listener would start the
        # poll timer at once, and a poll would then fall within a startup longer than the
        # interval, which the wait for startup avoids. async_first_check sets the interval.
        self._interval = timedelta(seconds=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=None,
        )
        self._session = async_get_clientsession(hass)
        self._ffmpeg: str = get_ffmpeg_manager(hass).binary
        self._semaphore = probe_semaphore(hass)
        self._url: str = entry.data[CONF_URL]
        self._stream: str = entry.data[CONF_STREAM]
        self._access = rtsp_access(entry.data)
        self._api = api_access(entry.data)
        # A stream name can be a camera address with a password, so only this
        # form goes to the device registry and diagnostics.
        self.stream_label: str = stream_label(self._stream)
        self._timeout: int = options.get(CONF_FRAME_TIMEOUT, DEFAULT_FRAME_TIMEOUT)
        self._limits = Limits(
            failures=options.get(CONF_FAILURES, DEFAULT_FAILURES),
            same_frames=options.get(CONF_SAME_FRAMES, DEFAULT_SAME_FRAMES),
        )
        # Bumped by every monitoring switch: a check started before the switch is discarded.
        self._generation = 0
        # The first check runs in the background, so entities need a state before it.
        # A camera in planned shutdown is known to be off without any check.
        self.data = CameraState() if self.monitoring else disabled_state(CameraState())

    @property
    def monitoring(self) -> bool:
        """Return whether monitoring is on."""
        return self.config_entry.data.get(CONF_MONITORING, True)

    def async_set_monitoring(self, enabled: bool) -> None:
        """Change the planned shutdown flag; turning on starts a check in the background.

        The flag lives in the config entry, not in the entity state: it is needed
        before the first poll, otherwise a restart would have to query the camera.
        Until the check started by turning on ends, the camera state is unknown;
        while Home Assistant starts, that check waits until startup is over.
        Switching to the current value changes nothing.
        """
        if enabled == self.monitoring:
            return
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MONITORING: enabled}
        )
        self._generation += 1
        if enabled:
            self.async_set_updated_data(CameraState(last_frame_at=self.data.last_frame_at))
            # While Home Assistant starts, the check waits like the first one does:
            # the first check after startup covers this camera too.
            if self.update_interval is not None:
                self._start_check()
        else:
            # Turning off needs no request, so it does not wait for a running check.
            self.async_set_updated_data(self._disabled(self.data))

    @callback
    def async_restore(self, saved: Mapping[str, Any]) -> None:
        """Take camera state an entity kept across a restart or a reload of the entry.

        Only before the first check and while monitoring is on: a check result is
        newer, and turning monitoring off and on starts counting over.
        """
        if self.monitoring and self.data.status is None:
            self.data = replace(self.data, **saved)

    async def async_check_now(self) -> None:
        """Run an out-of-schedule check and wait for it.

        The check runs as a task of the entry: cancelling the caller (a script
        stopped mid-check) does not cancel the check, which would otherwise mark
        the entities unavailable until the next poll.

        If monitoring was switched off and on during the wait, the check started
        by turning on stands in for this one, so the call ends without an error.
        It does not wait for that check: until it ends, the state is unknown.

        Raises:
            ServiceValidationError: Monitoring is off, also when it was turned off
                during the check.
            HomeAssistantError: The entry was reloaded or unloaded during the
                check (options saved, address changed), which stopped it.
        """
        if self.monitoring:
            try:
                await asyncio.shield(self._start_check())
            except asyncio.CancelledError:
                # The caller itself is being cancelled: let it go.
                if (task := asyncio.current_task()) is not None and task.cancelling():
                    raise
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="check_cancelled"
                ) from None
            if self.monitoring:
                return
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="monitoring_off")

    @callback
    def async_first_check(self) -> None:
        """Start the regular schedule with a check in the background.

        The check runs in the background: it can take the whole frame timeout, and
        longer while other cameras hold the check slots, so the setup of an entry
        added or reloaded after startup must not wait for it. When it ends, the next
        poll is planned one interval later.
        """
        self.update_interval = self._interval
        self._start_check()

    def _start_check(self) -> asyncio.Task[None]:
        return self.config_entry.async_create_background_task(
            self.hass, self.async_refresh(), f"{DOMAIN} check {self.config_entry.entry_id}"
        )

    def _disabled(self, state: CameraState) -> CameraState:
        new_state = disabled_state(state)
        self._log_transition(state, new_state, "monitoring is off")
        return new_state

    @asynccontextmanager
    async def _slot(self, generation: int) -> AsyncIterator[None]:
        """Take a semaphore slot, then give up if monitoring was switched while waiting."""
        async with self._semaphore:
            if generation != self._generation:
                raise _Switched
            yield

    async def _async_update_data(self) -> CameraState:
        """Run one camera check."""
        if not self.monitoring:
            return self._disabled(self.data)

        generation = self._generation
        # Only the ffmpeg run takes a semaphore slot and is timed, so waiting for
        # other cameras does not count as response time.
        try:
            result = await probe_frame(
                self._session,
                self._url,
                self._stream,
                self._ffmpeg,
                self._timeout,
                self._slot(generation),
                access=self._access,
                api=self._api,
            )
        except _Switched:
            return self.data

        # Monitoring may have been switched, even off and on again, during the
        # check: its result then belongs to a state that no longer exists.
        if generation != self._generation:
            return self.data

        state = self.data
        new_state = apply_result(state, result, self._limits, dt_util.utcnow())
        self._log_transition(state, new_state, result.detail)
        return new_state

    def _log_transition(self, old: CameraState, new: CameraState, detail: str) -> None:
        """Log a change of status or connection at INFO; repeats go to DEBUG only.

        The camera is named by the entry title: the stream label of streams named
        by address is the same for all of them.
        """
        changed = new.status is not old.status or new.connected is not old.connected
        _LOGGER.log(
            logging.INFO if changed else logging.DEBUG,
            "%s: status %s, connected %s (%s)",
            self.config_entry.title,
            new.status,
            new.connected,
            detail,
        )


class _Switched(Exception):
    """Monitoring was switched while a check waited for its slot."""
