"""Entities, check loop and security on a real Home Assistant."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import CoreState, State, is_callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.frame_pulse import async_migrate_entry
from custom_components.frame_pulse.const import (
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_FAILURES,
    CONF_MONITORING,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_STREAM,
    DOMAIN,
)
from custom_components.frame_pulse.coordinator import (
    FramePulseCoordinator,
    entry_unique_id,
    probe_semaphore,
)
from custom_components.frame_pulse.diagnostics import async_get_config_entry_diagnostics
from custom_components.frame_pulse.probe import MAX_DETAIL_CHARS, MAX_INFO_BYTES, Grab

from .conftest import (
    API_INFO,
    API_URL,
    NOT_FOUND,
    RTSP_URL,
    SECRET_URL,
    STREAM,
    STREAMS_URL,
    URL,
    Ffmpeg,
    jpeg,
)

INTERVAL = timedelta(seconds=60)


class Camera:
    """Controllable go2rtc and ffmpeg: sets the outcome of the next frame grab."""

    def __init__(self, mock, ffmpeg: Ffmpeg) -> None:
        self.mock = mock
        self.ffmpeg = ffmpeg

    def go2rtc(self, info=API_INFO, streams=None, **api) -> None:
        self.mock.clear_requests()
        self.mock.get(API_URL, **(api or {"json": info}))
        self.mock.get(STREAMS_URL, json={STREAM: {}} if streams is None else streams)

    def frame(self, body: bytes = b"", returncode: int | None = 0, error: str = "") -> None:
        self.go2rtc()
        self.ffmpeg.outcome = Grab(returncode, body, error)

    @property
    def frame_requests(self) -> int:
        return len(self.ffmpeg.urls)


@pytest.fixture
def camera(aioclient_mock, ffmpeg) -> Camera:
    return Camera(aioclient_mock, ffmpeg)


async def setup(hass, entry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def tick(hass, entry) -> None:
    """Run one check the way a scheduled poll does, without moving the clock."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


def ids(hass, entry) -> dict[str, str]:
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    return {e.unique_id.removeprefix(f"{entry.entry_id}_"): e.entity_id for e in entries}


def state(hass, entry, key: str) -> str:
    return hass.states.get(ids(hass, entry)[key]).state


def extra_entries(hass, count: int) -> list[MockConfigEntry]:
    entries = []
    for number in range(count):
        extra = MockConfigEntry(
            domain=DOMAIN,
            unique_id=entry_unique_id(URL, f"stream{number}"),
            data={CONF_URL: URL, CONF_STREAM: f"stream{number}"},
        )
        extra.add_to_hass(hass)
        entries.append(extra)
    return entries


async def test_all_entities_for_working_camera(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)

    assert set(ids(hass, entry)) == {
        "connectivity",
        "frozen",
        "status",
        "last_frame",
        "response_time",
        "monitoring",
        "check_now",
    }
    assert state(hass, entry, "status") == "ok"
    assert state(hass, entry, "connectivity") == STATE_ON
    assert state(hass, entry, "frozen") == STATE_OFF
    assert state(hass, entry, "monitoring") == STATE_ON
    assert state(hass, entry, "last_frame") not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert camera.ffmpeg.urls == [RTSP_URL]

    registry = er.async_get(hass)
    categories = {
        key: registry.async_get(entity_id).entity_category
        for key, entity_id in ids(hass, entry).items()
    }
    assert categories == {
        "connectivity": None,
        "frozen": None,
        "status": None,
        "last_frame": EntityCategory.DIAGNOSTIC,
        "response_time": EntityCategory.DIAGNOSTIC,
        "monitoring": EntityCategory.CONFIG,
        "check_now": None,
    }


@pytest.mark.parametrize(
    ("grab", "streams", "expected"),
    [
        (Grab(0, b""), None, "no_stream"),
        (Grab(0, b"not a jpeg"), None, "bad_frame"),
        (Grab(None), None, "frame_timeout"),
        (Grab(1, b"", "Invalid data found when processing input"), None, "no_stream"),
        # go2rtc answers 404 both for a camera it cannot reach and for an unknown stream.
        (Grab(8, b"", NOT_FOUND), None, "no_stream"),
        (Grab(8, b"", NOT_FOUND), {"other": {}}, "stream_not_found"),
        # The check cannot reach the go2rtc RTSP server: the camera is not to blame.
        (Grab(195, b"", "Error opening input files: Connection refused"), None, "probe_error"),
        (
            Grab(1, b"", "Server returned 401 Unauthorized (authorization failed)"),
            None,
            "probe_error",
        ),
        (FileNotFoundError(2, "No such file or directory"), None, "probe_error"),
    ],
)
async def test_status_table_with_live_go2rtc(hass, entry, camera, grab, streams, expected):
    camera.go2rtc(streams=streams)
    camera.ffmpeg.outcome = grab
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, entry, "status") == expected


@pytest.mark.parametrize(
    "api",
    [
        {"exc": aiohttp.ClientConnectionError()},
        {"exc": TimeoutError()},
        {"status": 404},
        {"text": "<html>not go2rtc</html>"},
        {"json": {"version": "1.9.9", "rtsp": {"listen": ""}}},
        {"json": {"version": "1.9.9"}},
        {"json": {"streams": {}}},
        {"json": {**API_INFO, "padding": "x" * (MAX_INFO_BYTES + 1)}},
    ],
)
async def test_probe_error_without_ffmpeg_when_go2rtc_cannot_serve(hass, entry, camera, api):
    """No answer, another service on the port or the RTSP server off: ffmpeg is not run."""
    camera.go2rtc(**api)
    await setup(hass, entry)

    assert state(hass, entry, "status") == "probe_error"
    assert state(hass, entry, "connectivity") == STATE_UNAVAILABLE
    assert state(hass, entry, "frozen") == STATE_UNAVAILABLE
    assert camera.frame_requests == 0


@pytest.mark.parametrize(
    "grab", [Grab(0, b""), Grab(None), Grab(8, b"", NOT_FOUND)], ids=["empty", "hang", "404"]
)
async def test_probe_error_when_go2rtc_dies_during_check(hass, entry, camera, caplog, grab):
    caplog.set_level(logging.INFO, logger="custom_components.frame_pulse")
    camera.frame()
    camera.ffmpeg.outcome = grab

    async def go2rtc_dies() -> None:
        camera.mock.clear_requests()
        camera.mock.get(API_URL, exc=aiohttp.ClientConnectionError())
        camera.mock.get(STREAMS_URL, exc=aiohttp.ClientConnectionError())

    camera.ffmpeg.hook = go2rtc_dies
    await setup(hass, entry)

    assert state(hass, entry, "status") == "probe_error"
    # The reason of the failed frame is kept next to the API failure.
    assert "(after " in caplog.text
    if grab.returncode is None:
        assert "no frame in 20 s" in caplog.text


async def test_reason_after_api_failure_is_cut(hass, entry, camera, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.frame_pulse")
    camera.frame(b"", 1, "E" * 500)

    async def go2rtc_breaks() -> None:
        camera.mock.clear_requests()
        camera.mock.get(API_URL, text="not go2rtc")

    camera.ffmpeg.hook = go2rtc_breaks
    await setup(hass, entry)

    reasons = [r.args[-1] for r in caplog.records if r.name.startswith("custom_components.")]
    reasons = [r for r in reasons if str(r).startswith("go2rtc API failed")]
    assert reasons
    assert all(len(r) <= MAX_DETAIL_CHARS for r in reasons)


async def test_requests_use_the_configured_port(hass, entry, camera):
    """The HTTP mock ignores the port, so the requested addresses are checked here."""
    camera.go2rtc()
    camera.ffmpeg.outcome = Grab(8, b"", NOT_FOUND)
    await setup(hass, entry)

    urls = [call[1] for call in camera.mock.mock_calls]
    assert {url.path for url in urls} == {"/api", "/api/streams"}
    assert {url.port for url in urls} == {1984}


async def test_stream_not_found_keeps_binary_sensors(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_ON

    camera.go2rtc(streams={"other": {}})
    camera.ffmpeg.outcome = Grab(8, b"", NOT_FOUND)
    await tick(hass, entry)

    assert state(hass, entry, "status") == "stream_not_found"
    assert state(hass, entry, "connectivity") == STATE_ON
    assert state(hass, entry, "frozen") == STATE_OFF


async def test_response_time_unknown_when_ffmpeg_did_not_run(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    assert state(hass, entry, "response_time") != STATE_UNKNOWN

    camera.go2rtc(exc=aiohttp.ClientConnectionError())
    await tick(hass, entry)

    assert state(hass, entry, "status") == "probe_error"
    assert state(hass, entry, "response_time") == STATE_UNKNOWN


async def test_threshold_then_recovery(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)

    camera.frame(b"")
    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_ON

    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_OFF

    camera.frame(jpeg(1))
    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_ON


async def test_frame_timeout_is_a_failure_and_every_check_runs(hass, entry, camera):
    """A hanging camera is checked on every cycle: a killed ffmpeg leaves nothing behind."""
    camera.frame(jpeg())
    await setup(hass, entry)
    camera.frame(returncode=None)

    await tick(hass, entry)
    assert state(hass, entry, "status") == "frame_timeout"
    assert state(hass, entry, "connectivity") == STATE_ON

    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_OFF
    assert camera.frame_requests == 3


async def test_frozen_sensor(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    await tick(hass, entry)
    assert state(hass, entry, "frozen") == STATE_OFF

    await tick(hass, entry)
    assert state(hass, entry, "status") == "frozen"
    assert state(hass, entry, "frozen") == STATE_ON
    assert state(hass, entry, "connectivity") == STATE_ON


async def test_check_button_raises_when_monitoring_is_off(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    entry.runtime_data.async_set_monitoring(False)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "button", "press", {"entity_id": ids(hass, entry)["check_now"]}, blocking=True
        )
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "monitoring_off"
    # Home Assistant drops the final period of a translated message.
    assert str(err.value) == "Monitoring is off. Turn it on to check the camera"


async def test_turning_off_does_not_wait_for_running_check(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    started = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        started.set()
        await release.wait()

    camera.frame(jpeg(1))
    camera.ffmpeg.hook = hold
    running = hass.async_create_task(coordinator.async_refresh())
    await started.wait()
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": ids(hass, entry)["monitoring"]}, blocking=True
    )
    assert state(hass, entry, "status") == "disabled"
    assert state(hass, entry, "monitoring") == STATE_OFF

    release.set()
    await running
    await hass.async_block_till_done()

    # The result of the check that was running must not undo the shutdown.
    assert state(hass, entry, "status") == "disabled"


async def test_response_time_excludes_queue(hass, entry, camera):
    """Waiting for other cameras is not part of the response time."""
    camera.frame(jpeg())

    async def slow() -> None:
        await asyncio.sleep(0.05)

    camera.ffmpeg.hook = slow
    coordinators = [FramePulseCoordinator(hass, e) for e in [entry, *extra_entries(hass, 3)]]
    await asyncio.gather(*(c.async_refresh() for c in coordinators))

    assert max(c.data.duration_ms for c in coordinators) < 90


async def test_frozen_sensor_stays_on_through_failed_check(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    await tick(hass, entry)
    await tick(hass, entry)
    assert state(hass, entry, "frozen") == STATE_ON

    camera.frame(b"")
    await tick(hass, entry)
    assert state(hass, entry, "status") == "no_stream"
    assert state(hass, entry, "frozen") == STATE_ON

    camera.frame(jpeg(1))
    await tick(hass, entry)
    assert state(hass, entry, "status") == "ok"
    assert state(hass, entry, "frozen") == STATE_OFF


async def test_check_button(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    before = camera.frame_requests
    await hass.services.async_call(
        "button", "press", {"entity_id": ids(hass, entry)["check_now"]}, blocking=True
    )
    assert camera.frame_requests == before + 1


async def test_planned_shutdown(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    switch = ids(hass, entry)["monitoring"]

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    before = camera.frame_requests
    assert state(hass, entry, "status") == "disabled"
    assert state(hass, entry, "connectivity") == STATE_UNAVAILABLE
    assert state(hass, entry, "frozen") == STATE_UNAVAILABLE

    await tick(hass, entry)
    assert camera.frame_requests == before

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.frame_requests == before + 1
    assert state(hass, entry, "status") == "ok"


async def test_shutdown_survives_restart(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": ids(hass, entry)["monitoring"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    before = camera.frame_requests

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert camera.frame_requests == before
    assert state(hass, entry, "status") == "disabled"
    assert state(hass, entry, "monitoring") == STATE_OFF


async def test_lost_camera_stays_off_across_reload_and_then_comes_back_on(hass, entry, camera):
    """A camera that comes back after a reload turns the connection from `off` to `on`."""
    camera.frame(b"")
    await setup(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_UNKNOWN

    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_OFF

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "connectivity") == STATE_OFF

    camera.frame(jpeg())
    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_ON


async def test_failure_count_survives_reload(hass, entry, camera):
    """One failure before the reload and one after reach the threshold of two."""
    camera.frame(jpeg())
    await setup(hass, entry)
    camera.frame(b"")
    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_ON

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "connectivity") == STATE_OFF


async def test_frozen_picture_stays_frozen_across_reload(hass, entry, camera):
    """The frame digest is kept: the same frame after a reload is no new picture."""
    camera.frame(jpeg())
    await setup(hass, entry)
    await tick(hass, entry)
    await tick(hass, entry)
    assert state(hass, entry, "frozen") == STATE_ON

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "frozen") == STATE_ON
    assert state(hass, entry, "status") == "frozen"

    camera.frame(jpeg(seed=1))
    await tick(hass, entry)
    assert state(hass, entry, "frozen") == STATE_OFF


async def test_connection_restored_after_restart_before_first_check(hass, entry, camera):
    """Until startup is over the sensor shows the kept value, then the check goes on from it."""
    restore_binary_sensors(hass, entry, connected=False, failures=2)
    camera.frame(jpeg())
    hass.set_state(CoreState.starting)
    await setup(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_OFF
    assert state(hass, entry, "frozen") == STATE_UNKNOWN
    assert state(hass, entry, "status") == STATE_UNKNOWN

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "connectivity") == STATE_ON


@pytest.mark.parametrize(
    "saved",
    [
        {"connected": "off", "failures": 2},
        {"connected": False, "failures": True},
        {"connected": False},
    ],
    ids=["text", "bool-counter", "missing"],
)
async def test_kept_state_of_wrong_type_is_ignored(hass, entry, camera, saved):
    restore_binary_sensors(hass, entry, **saved)
    camera.frame(b"")
    await setup(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_UNKNOWN


async def test_kept_state_ignored_while_monitoring_is_off(hass, entry, camera):
    restore_binary_sensors(hass, entry, connected=False, failures=2)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_MONITORING: False})
    await setup(hass, entry)
    assert entry.runtime_data.data.connected is None
    assert state(hass, entry, "connectivity") == STATE_UNAVAILABLE


async def test_sensor_enabled_after_a_check_keeps_the_checked_state(hass, entry, camera):
    """A kept value is older than any check: an entity added later does not bring it back."""
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    coordinator.async_restore({"connected": False})
    assert coordinator.data.connected is True


def restore_binary_sensors(hass, entry, **saved) -> None:
    registry = er.async_get(hass)
    for key in ("connectivity", "frozen"):
        registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            suggested_object_id=f"driveway_{key}",
            config_entry=entry,
        )
    mock_restore_cache_with_extra_data(
        hass,
        [(State("binary_sensor.driveway_connectivity", STATE_OFF), saved)],
    )


async def test_last_frame_restored_after_restart(hass, entry, camera):
    moment = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    entity_id = "sensor.driveway_last_frame"
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_frame",
        suggested_object_id="driveway_last_frame",
        config_entry=entry,
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(entity_id, moment.isoformat()),
                {
                    "native_value": {
                        "__type": "<class 'datetime.datetime'>",
                        "isoformat": moment.isoformat(),
                    },
                    "native_unit_of_measurement": None,
                },
            )
        ],
    )
    camera.frame(b"")
    await setup(hass, entry)

    restored = dt_util.parse_datetime(hass.states.get(entity_id).state)
    assert restored == moment


async def test_semaphore_limits_parallel_checks(hass, entry, camera):
    """Six cameras are checked at once, but at most two ffmpeg runs go in parallel."""
    camera.frame(jpeg())
    running = 0
    peak = 0

    async def count() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    camera.ffmpeg.hook = count
    entries = [entry, *extra_entries(hass, 5)]
    await asyncio.gather(*(FramePulseCoordinator(hass, e).async_refresh() for e in entries))

    assert peak == 2
    assert camera.frame_requests == 6


async def test_scheduled_polling(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    before = camera.frame_requests
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.frame_requests == before + 1


async def test_no_secrets_in_log_or_diagnostics(hass, entry, camera, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.frame_pulse")
    camera.frame(returncode=1, error=f"[rtsp @ 0x1] failed to open {SECRET_URL}")
    await setup(hass, entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "hunter2" not in caplog.text
    assert "rtsp://admin" not in caplog.text
    assert (
        "Driveway: status no_stream, connected None (ffmpeg 1: [rtsp @ 0x1] failed to open rtsp://***)"
        in (caplog.text)
    )
    assert "hunter2" not in str(diagnostics)
    assert "rtsp://" not in str(diagnostics)
    assert "go2rtc.test" not in str(diagnostics)
    assert diagnostics == {
        "entry": {CONF_URL: "**REDACTED**", CONF_STREAM: STREAM},
        "options": {"failures": 2, "same_frames": 3},
        "status": "no_stream",
        "connected": None,
        "failures": 1,
        "same_frames": 0,
        "frozen": False,
        "last_frame_at": None,
        "last_check_at": diagnostics["last_check_at"],
        "duration_ms": diagnostics["duration_ms"],
    }
    assert diagnostics["last_check_at"] is not None


async def test_error_text_carries_neither_the_stream_nor_a_long_reply(
    hass, aioclient_mock, ffmpeg, caplog
):
    """ffmpeg prints the RTSP address, which holds the stream name, in its errors."""
    caplog.set_level(logging.DEBUG, logger="custom_components.frame_pulse")
    stream = "tuya://host?device_id=1&client_secret=S3cr3t"
    secret_entry = MockConfigEntry(
        domain=DOMAIN, title="Yard", data={CONF_URL: URL, CONF_STREAM: stream}
    )
    secret_entry.add_to_hass(hass)
    camera = Camera(aioclient_mock, ffmpeg)
    camera.go2rtc(streams={stream: {}})
    camera.frame(returncode=1, error="Z" * 5000)
    await setup(hass, secret_entry)
    (url,) = ffmpeg.urls
    assert "S3cr3t" in url  # the name reaches go2rtc, URL-encoded

    camera.frame(returncode=1, error=f"Error opening input file {url}. {stream}")
    await tick(hass, secret_entry)

    assert "S3cr3t" not in caplog.text
    assert (
        "Yard: status no_stream, connected False (ffmpeg 1: Error opening input file rtsp://***"
        in caplog.text
    )
    assert "Z" * 300 not in caplog.text


async def test_repeated_error_logged_once_at_info(hass, entry, camera, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.frame_pulse")
    camera.frame(b"")
    await setup(hass, entry)
    await tick(hass, entry)
    await tick(hass, entry)
    await tick(hass, entry)
    lines = [
        r.getMessage()
        for r in caplog.records
        if "status no_stream" in r.getMessage() and r.levelno == logging.INFO
    ]
    # One line when the status changes, one when the failure threshold flips connected.
    assert [line.split(" (")[0] for line in lines] == [
        "Driveway: status no_stream, connected None",
        "Driveway: status no_stream, connected False",
    ]


async def test_setup_does_not_wait_for_first_check(hass, entry, camera):
    """A hanging camera must not hold up setup: the first check runs in the background."""
    camera.frame(jpeg())
    release = asyncio.Event()

    async def hold() -> None:
        await release.wait()

    camera.ffmpeg.hook = hold
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, entry, "status") == STATE_UNKNOWN
    assert state(hass, entry, "connectivity") == STATE_UNKNOWN
    assert state(hass, entry, "frozen") == STATE_UNKNOWN
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["status"] is None
    assert diagnostics["last_check_at"] is None

    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert state(hass, entry, "status") == "ok"


async def test_first_check_waits_for_home_assistant_start(hass, entry, camera):
    """While Home Assistant starts, go2rtc started by another integration may not answer yet."""
    camera.frame(jpeg())
    hass.set_state(CoreState.starting)
    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert camera.ffmpeg.urls == []
    assert state(hass, entry, "status") == STATE_UNKNOWN

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert len(camera.ffmpeg.urls) == 1
    assert state(hass, entry, "status") == "ok"


async def test_no_poll_during_long_startup(hass, entry, camera):
    """A startup longer than the interval makes no poll; polls start with the first check."""
    camera.frame(jpeg())
    hass.set_state(CoreState.starting)
    await setup(hass, entry)
    async_fire_time_changed(hass, dt_util.utcnow() + 3 * INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.ffmpeg.urls == []
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(camera.ffmpeg.urls) == 1
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL / 2)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(camera.ffmpeg.urls) == 1
    camera.frame(jpeg(1))
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL + timedelta(seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(camera.ffmpeg.urls) == 2


async def test_monitoring_on_during_startup_waits_for_startup(hass, entry, camera):
    """Turning monitoring on during startup checks only once startup is over."""
    camera.frame(jpeg())
    hass.set_state(CoreState.starting)
    await setup(hass, entry)
    switch = ids(hass, entry)["monitoring"]
    for service in ("turn_off", "turn_on"):
        await hass.services.async_call("switch", service, {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.ffmpeg.urls == []
    assert state(hass, entry, "status") == STATE_UNKNOWN

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(camera.ffmpeg.urls) == 1
    assert state(hass, entry, "status") == "ok"


async def test_check_now_during_startup_checks_at_once(hass, entry, camera):
    """The button is pressed on purpose, so it does not wait for startup."""
    camera.frame(jpeg())
    hass.set_state(CoreState.starting)
    await setup(hass, entry)
    await hass.services.async_call(
        "button", "press", {"entity_id": ids(hass, entry)["check_now"]}, blocking=True
    )
    assert len(camera.ffmpeg.urls) == 1
    assert state(hass, entry, "status") == "ok"


async def test_entry_unloaded_during_startup_makes_no_check(hass, entry, camera):
    """The wait for startup ends with the entry: no listener is left behind, no check runs."""
    camera.frame(jpeg())
    hass.set_state(CoreState.starting)
    before = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0)
    await setup(hass, entry)
    assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0) == before + 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_STARTED, 0) == before

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert camera.ffmpeg.urls == []


async def test_stream_name_with_credentials_is_masked(hass, camera, caplog):
    """go2rtc names a stream after its source address when it is requested by address."""
    caplog.set_level(logging.DEBUG, logger="custom_components.frame_pulse")
    secret_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Yard",
        unique_id=entry_unique_id(URL, SECRET_URL),
        data={CONF_URL: URL, CONF_STREAM: SECRET_URL},
    )
    secret_entry.add_to_hass(hass)
    camera.frame(b"")
    await setup(hass, secret_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, secret_entry)
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), secret_entry.entry_id)

    assert "status no_stream" in caplog.text
    assert "hunter2" not in caplog.text
    assert "hunter2" not in str(diagnostics)
    assert device.model == "rtsp://***"
    assert "hunter2" not in secret_entry.unique_id


async def test_silent_rtsp_port_after_frame_timeout_is_probe_error(hass, entry, camera, caplog):
    """A port that drops connections keeps ffmpeg waiting like a hanging camera does."""
    caplog.set_level(logging.INFO, logger="custom_components.frame_pulse")
    camera.frame(jpeg())
    await setup(hass, entry)
    camera.ffmpeg.outcome = Grab(None)
    camera.ffmpeg.port_problem = "no answer in 5 s"
    for _ in range(3):
        await tick(hass, entry)

    assert state(hass, entry, "status") == "probe_error"
    # The camera is not to blame: no failure is counted and the connection is kept.
    assert entry.runtime_data.data.failures == 0
    assert entry.runtime_data.data.connected is True
    assert camera.ffmpeg.port_tries == [RTSP_URL] * 3
    assert (
        "the go2rtc RTSP port does not answer: no answer in 5 s (after no frame in 20 s)"
        in caplog.text
    )


async def test_rtsp_port_tried_only_after_frame_timeout(hass, entry, camera):
    camera.frame(b"")
    camera.ffmpeg.port_problem = "Connection refused"
    await setup(hass, entry)
    assert state(hass, entry, "status") == "no_stream"

    camera.ffmpeg.outcome = Grab(None)
    camera.ffmpeg.port_problem = None
    await tick(hass, entry)
    assert state(hass, entry, "status") == "frame_timeout"
    assert camera.ffmpeg.port_tries == [RTSP_URL]


async def test_rtsp_port_not_tried_when_go2rtc_api_died(hass, entry, camera, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.frame_pulse")
    camera.frame(b"", None)
    camera.ffmpeg.port_problem = "no answer in 5 s"

    async def go2rtc_dies() -> None:
        camera.go2rtc(exc=aiohttp.ClientConnectionError())

    camera.ffmpeg.hook = go2rtc_dies
    await setup(hass, entry)

    assert state(hass, entry, "status") == "probe_error"
    assert "go2rtc API failed: no response (after no frame in 20 s)" in caplog.text
    assert camera.ffmpeg.port_tries == []


async def test_rtsp_port_and_login_reach_ffmpeg_and_stay_hidden(hass, camera, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.frame_pulse")
    login_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gate",
        unique_id=entry_unique_id(URL, STREAM),
        data={
            CONF_URL: URL,
            CONF_STREAM: STREAM,
            CONF_RTSP_PORT: 18554,
            CONF_RTSP_USERNAME: "viewer",
            CONF_RTSP_PASSWORD: "s3cr3t@pw",
        },
    )
    login_entry.add_to_hass(hass)
    # ffmpeg 6.0 and older put the address, login included, into the last line.
    camera.frame(
        b"", 1, "rtsp://viewer:s3cr3t%40pw@go2rtc.test:18554/driveway_sd?video: 401 Unauthorized"
    )
    await setup(hass, login_entry)

    assert camera.ffmpeg.urls == [f"rtsp://viewer:s3cr3t%40pw@go2rtc.test:18554/{STREAM}?video"]
    assert state(hass, login_entry, "status") == "probe_error"
    diagnostics = await async_get_config_entry_diagnostics(hass, login_entry)
    assert diagnostics["entry"][CONF_RTSP_PORT] == 18554
    assert diagnostics["entry"][CONF_RTSP_USERNAME] == "**REDACTED**"
    assert diagnostics["entry"][CONF_RTSP_PASSWORD] == "**REDACTED**"
    for secret in ("s3cr3t", "viewer"):
        assert secret not in caplog.text
        assert secret not in str(diagnostics)


@pytest.fixture
def api_entry(hass) -> MockConfigEntry:
    """An entry with a go2rtc API login."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gate",
        unique_id=entry_unique_id(URL, STREAM),
        data={
            CONF_URL: URL,
            CONF_STREAM: STREAM,
            CONF_API_USERNAME: "api-user",
            CONF_API_PASSWORD: "api-s3cr3t",
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


# Base64 of "api-user:api-s3cr3t".
API_HEADER = {"Authorization": "Basic YXBpLXVzZXI6YXBpLXMzY3IzdA=="}


def sent_headers(camera: Camera) -> list[dict]:
    return [call[3] or {} for call in camera.mock.mock_calls]


async def test_every_check_sends_the_api_login(hass, api_entry, camera):
    """The version before the frame and the re-checks after a failure."""
    calls = []
    camera.frame(jpeg())
    await setup(hass, api_entry)
    calls += camera.mock.mock_calls
    camera.frame(b"", 8, NOT_FOUND)
    await tick(hass, api_entry)
    calls += camera.mock.mock_calls
    camera.frame(b"")
    await tick(hass, api_entry)
    calls += camera.mock.mock_calls

    assert [call[1].path for call in calls] == ["/api", "/api", "/api/streams", "/api", "/api"]
    assert [call[3] for call in calls] == [API_HEADER] * len(calls)
    assert all("api-user" not in url and "api-s3cr3t" not in url for url in camera.ffmpeg.urls)


async def test_entry_without_an_api_login_sends_none(hass, entry, camera):
    """Entries saved before the API login existed have no keys and need no migration."""
    camera.frame(jpeg())
    await setup(hass, entry)
    assert state(hass, entry, "status") == "ok"
    assert sent_headers(camera)
    assert all("Authorization" not in headers for headers in sent_headers(camera))


def take_auth_error_log(caplog) -> str:
    """Return and drop the error line Home Assistant logs when a check raises an auth failure.

    The line is expected here, so it must not fail the test through `no_unexpected_errors`.
    """
    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    caplog.records[:] = [r for r in caplog.records if r is not record]
    return record.getMessage()


def reauth_flows(hass, entry) -> list:
    return list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_refused_api_login_asks_for_reauth(hass, api_entry, camera, caplog):
    """A 401 from the go2rtc API stops polling and starts reauthentication."""
    caplog.set_level(logging.DEBUG, logger="custom_components.frame_pulse")
    camera.go2rtc(status=401)
    await setup(hass, api_entry)

    assert camera.frame_requests == 0
    assert not api_entry.runtime_data.last_update_success
    for key in ("status", "connectivity", "frozen", "check_now"):
        assert state(hass, api_entry, key) == STATE_UNAVAILABLE, key
    # A planned shutdown can still be marked.
    assert state(hass, api_entry, "monitoring") == STATE_ON
    assert len(reauth_flows(hass, api_entry)) == 1
    message = take_auth_error_log(caplog)
    assert message.endswith(
        "go2rtc refused the API login. Enter the current login when Home Assistant "
        "asks for it in Settings > Devices & services"
    )
    # No poll is planned until the login is fixed.
    requests = camera.mock.call_count
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL * 3)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.mock.call_count == requests

    diagnostics = await async_get_config_entry_diagnostics(hass, api_entry)
    assert diagnostics["entry"][CONF_API_USERNAME] == "**REDACTED**"
    assert diagnostics["entry"][CONF_API_PASSWORD] == "**REDACTED**"
    for secret in ("api-user", "api-s3cr3t"):
        assert secret not in caplog.text
        assert secret not in str(diagnostics)
        assert secret not in api_entry.unique_id
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), api_entry.entry_id)
    assert "api-user" not in f"{device.name} {device.model}"


async def test_reauth_with_the_new_login_resumes_checks(hass, api_entry, camera, caplog):
    camera.go2rtc(status=401)
    await setup(hass, api_entry)
    take_auth_error_log(caplog)
    (flow,) = reauth_flows(hass, api_entry)

    camera.frame(jpeg())
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_USERNAME: "api-user", CONF_PASSWORD: "new-s3cr3t"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reauth_successful"
    assert api_entry.data[CONF_API_PASSWORD] == "new-s3cr3t"
    # The reloaded entry sends the new login: base64 of "api-user:new-s3cr3t".
    assert sent_headers(camera)[-1] == {"Authorization": "Basic YXBpLXVzZXI6bmV3LXMzY3IzdA=="}
    assert api_entry.runtime_data.last_update_success
    assert state(hass, api_entry, "status") == "ok"
    assert not reauth_flows(hass, api_entry)


async def test_api_login_refused_after_a_failed_frame_asks_for_reauth(
    hass, api_entry, camera, caplog
):
    """The re-check of go2rtc after a failed frame meets a 401."""
    camera.frame(jpeg())
    await setup(hass, api_entry)
    camera.frame(b"")

    async def login_changes() -> None:
        camera.mock.clear_requests()
        camera.mock.get(API_URL, status=401)

    camera.ffmpeg.hook = login_changes
    await tick(hass, api_entry)

    take_auth_error_log(caplog)
    assert state(hass, api_entry, "status") == STATE_UNAVAILABLE
    assert len(reauth_flows(hass, api_entry)) == 1


async def test_check_now_reports_a_refused_api_login(hass, api_entry, camera, caplog):
    camera.frame(jpeg())
    await setup(hass, api_entry)
    camera.go2rtc(status=401)

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button", "press", {"entity_id": ids(hass, api_entry)["check_now"]}, blocking=True
        )
    assert not isinstance(err.value, ServiceValidationError)
    assert err.value.translation_key == "api_auth_failed"
    take_auth_error_log(caplog)
    assert len(reauth_flows(hass, api_entry)) == 1


async def test_check_now_after_a_refused_login_is_fixed_in_go2rtc(hass, api_entry, camera, caplog):
    """go2rtc takes the saved login again: polling resumes and Check now raises no stale error.

    While the login is refused, Check now is unavailable like every entity of the
    camera but the Monitoring switch, so one way back without the reauthentication
    form is `homeassistant.update_entity`. Home Assistant keeps the last exception after
    that success, which a later press must not report again.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    camera.go2rtc(status=401)
    await setup(hass, api_entry)
    take_auth_error_log(caplog)

    camera.frame(jpeg())
    status = ids(hass, api_entry)["status"]
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": status}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, api_entry, "status") == "ok"
    before = camera.frame_requests
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.frame_requests == before + 1

    await hass.services.async_call(
        "button", "press", {"entity_id": ids(hass, api_entry)["check_now"]}, blocking=True
    )
    assert camera.frame_requests == before + 2
    assert state(hass, api_entry, "status") == "ok"


async def test_monitoring_switch_works_while_reauth_is_pending(hass, api_entry, camera, caplog):
    """Off marks a planned shutdown; on runs one check, and the login request is not repeated.

    After go2rtc takes the saved login again, turning on resumes polling.
    """
    camera.go2rtc(status=401)
    await setup(hass, api_entry)
    take_auth_error_log(caplog)
    coordinator = api_entry.runtime_data
    switch = ids(hass, api_entry)["monitoring"]

    requests = camera.mock.call_count
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, api_entry, "monitoring") == STATE_OFF
    assert state(hass, api_entry, "status") == "disabled"
    assert api_entry.options[CONF_MONITORING] is False
    assert len(reauth_flows(hass, api_entry)) == 1
    # Turning off plans polling again; those polls send no request.
    caplog.set_level(logging.DEBUG, logger="custom_components.frame_pulse")
    polls = caplog.text.count("(monitoring is off)")
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL * 3)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert caplog.text.count("(monitoring is off)") == polls + 1
    assert coordinator.last_update_success
    assert camera.mock.call_count == requests

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.mock.call_count == requests + 1
    assert camera.frame_requests == 0
    take_auth_error_log(caplog)
    assert coordinator.auth_failed
    assert state(hass, api_entry, "monitoring") == STATE_ON
    assert state(hass, api_entry, "status") == STATE_UNAVAILABLE
    assert state(hass, api_entry, "check_now") == STATE_UNAVAILABLE
    assert len(reauth_flows(hass, api_entry)) == 1
    # The refused check plans no poll.
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL * 6)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.mock.call_count == requests + 1

    camera.frame(jpeg())
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, api_entry, "status") == "ok"
    assert not coordinator.auth_failed
    assert camera.frame_requests == 1
    async_fire_time_changed(hass, dt_util.utcnow() + INTERVAL * 7)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.frame_requests == 2


async def test_monitoring_switch_is_unavailable_after_other_failures(hass, entry, camera, caplog):
    """Only a refused login keeps the switch available; polling goes on after other failures."""
    camera.frame(jpeg())
    await setup(hass, entry)

    async def broken() -> None:
        raise RuntimeError("bug")

    camera.ffmpeg.hook = broken
    await tick(hass, entry)

    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record.getMessage().startswith("Unexpected error fetching")
    caplog.records[:] = [r for r in caplog.records if r is not record]
    assert not entry.runtime_data.auth_failed
    assert state(hass, entry, "monitoring") == STATE_UNAVAILABLE
    assert state(hass, entry, "status") == STATE_UNAVAILABLE


async def test_monitoring_switch_follows_a_failure_of_the_other_kind(
    hass, api_entry, camera, caplog
):
    """Home Assistant skips the entity update after a failure that follows a failure.

    The switch still turns available after a refused login that follows another
    failure, and unavailable after another failure that follows a refused login.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    camera.frame(jpeg())
    await setup(hass, api_entry)

    async def broken() -> None:
        raise RuntimeError("bug")

    camera.ffmpeg.hook = broken
    await tick(hass, api_entry)
    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record.getMessage().startswith("Unexpected error fetching")
    caplog.records[:] = [r for r in caplog.records if r is not record]
    assert state(hass, api_entry, "monitoring") == STATE_UNAVAILABLE

    camera.go2rtc(status=401)
    await tick(hass, api_entry)
    assert api_entry.runtime_data.auth_failed
    assert len(reauth_flows(hass, api_entry)) == 1
    assert state(hass, api_entry, "monitoring") == STATE_ON
    assert state(hass, api_entry, "status") == STATE_UNAVAILABLE

    camera.go2rtc()
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": ids(hass, api_entry)["status"]},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    (record,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert record.getMessage().startswith("Unexpected error fetching")
    caplog.records[:] = [r for r in caplog.records if r is not record]
    assert not api_entry.runtime_data.auth_failed
    assert state(hass, api_entry, "monitoring") == STATE_UNAVAILABLE


class Gate:
    """Holds the first `count` ffmpeg runs until released."""

    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> None:
        if self.count > 0:
            self.count -= 1
            self.started.set()
            await self.release.wait()


async def test_cancelled_check_now_keeps_entities_available(hass, entry, camera):
    """A script stopped mid-check must not cancel the check and blank the entities."""
    camera.frame(jpeg())
    await setup(hass, entry)
    gate = Gate()
    camera.frame(jpeg(1))
    camera.ffmpeg.hook = gate

    caller = hass.async_create_task(entry.runtime_data.async_check_now())
    await gate.started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    gate.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.runtime_data.last_update_success
    assert state(hass, entry, "status") == "ok"
    assert state(hass, entry, "connectivity") == STATE_ON


async def test_presses_during_a_press_check_share_it(hass, entry, camera):
    """Repeated presses join the running check instead of queuing one check each."""
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    gate = Gate()
    camera.ffmpeg.hook = gate
    before = camera.frame_requests

    presses = [hass.async_create_task(coordinator.async_check_now()) for _ in range(3)]
    await gate.started.wait()
    gate.release.set()
    await asyncio.gather(*presses)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert camera.frame_requests == before + 1

    # Once that check is over, a press starts a new one.
    await coordinator.async_check_now()
    assert camera.frame_requests == before + 2


async def test_press_after_off_and_on_does_not_join_the_stale_check(hass, entry, camera):
    """The check of an earlier press is thrown away after the switch, so a new press checks."""
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    gate = Gate()
    camera.ffmpeg.hook = gate
    before = camera.frame_requests

    stale = hass.async_create_task(coordinator.async_check_now())
    await gate.started.wait()
    coordinator.async_set_monitoring(False)
    coordinator.async_set_monitoring(True)
    fresh = hass.async_create_task(coordinator.async_check_now())
    await asyncio.sleep(0)
    gate.release.set()
    await asyncio.gather(stale, fresh)
    await hass.async_block_till_done(wait_background_tasks=True)

    # The stale press check, the check of turning on and the new press check.
    assert camera.frame_requests == before + 3
    assert state(hass, entry, "status") == "ok"


async def test_check_now_after_off_and_on_while_waiting_ends_quietly(hass, entry, camera):
    """Monitoring is on again and the turn-on check ran: no "monitoring is off" error."""
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    gate = Gate()
    camera.ffmpeg.hook = gate
    running = hass.async_create_task(coordinator.async_refresh())
    await gate.started.wait()
    before = camera.frame_requests

    waiting = hass.async_create_task(coordinator.async_check_now())
    await asyncio.sleep(0)
    coordinator.async_set_monitoring(False)
    coordinator.async_set_monitoring(True)
    gate.release.set()
    await running
    await waiting
    await hass.async_block_till_done(wait_background_tasks=True)

    assert camera.frame_requests > before
    assert state(hass, entry, "status") == "ok"


async def test_check_now_raises_when_monitoring_turned_off_during_check(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    gate = Gate()
    camera.ffmpeg.hook = gate

    caller = hass.async_create_task(coordinator.async_check_now())
    await gate.started.wait()
    coordinator.async_set_monitoring(False)
    gate.release.set()
    with pytest.raises(ServiceValidationError) as err:
        await caller
    assert err.value.translation_key == "monitoring_off"
    assert state(hass, entry, "status") == "disabled"


async def test_check_now_reports_reload_during_check(hass, entry, camera):
    """Saving options reloads the entry and stops the check: the caller gets an error."""
    camera.frame(jpeg())
    await setup(hass, entry)
    gate = Gate()
    camera.ffmpeg.hook = gate

    press = hass.async_create_task(
        hass.services.async_call(
            "button", "press", {"entity_id": ids(hass, entry)["check_now"]}, blocking=True
        )
    )
    await gate.started.wait()
    await hass.config_entries.async_reload(entry.entry_id)
    with pytest.raises(HomeAssistantError) as err:
        await press
    assert not isinstance(err.value, ServiceValidationError)
    assert err.value.translation_key == "check_cancelled"
    message = "The check was stopped because the camera entry was reloaded. Try again"
    assert str(err.value) == message
    gate.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "status") == "ok"


async def test_no_ffmpeg_run_after_turning_off_while_waiting_for_a_slot(hass, entry, camera):
    camera.frame(jpeg())
    others = [FramePulseCoordinator(hass, e) for e in extra_entries(hass, 2)]
    gate = Gate(2)
    camera.ffmpeg.hook = gate
    # Untracked tasks: setup waits for tracked ones, and these are held on purpose.
    busy = [asyncio.create_task(c.async_refresh()) for c in others]
    await gate.started.wait()
    await asyncio.sleep(0)
    assert len(camera.ffmpeg.urls) == 2  # both slots are taken

    await setup_without_waiting(hass, entry)
    entry.runtime_data.async_set_monitoring(False)
    gate.release.set()
    await asyncio.gather(*busy)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not [url for url in camera.ffmpeg.urls if STREAM in url]
    assert state(hass, entry, "status") == "disabled"


async def setup_without_waiting(hass, entry) -> None:
    """Set up the entry while its first check is left in the background."""
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_turning_on_returns_at_once_with_unknown_state(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    switch = ids(hass, entry)["monitoring"]
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    gate = Gate()
    camera.ffmpeg.hook = gate

    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert state(hass, entry, "monitoring") == STATE_ON
    assert state(hass, entry, "status") == STATE_UNKNOWN
    assert state(hass, entry, "connectivity") == STATE_UNKNOWN
    assert hass.states.get(ids(hass, entry)["last_frame"]).state != STATE_UNKNOWN

    gate.release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "status") == "ok"


def test_set_monitoring_is_marked_as_a_callback():
    assert is_callback(FramePulseCoordinator.async_set_monitoring)


async def test_monitoring_flag_is_saved_in_the_options_without_a_reload(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    switch = ids(hass, entry)["monitoring"]

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.options[CONF_MONITORING] is False
    assert CONF_MONITORING not in entry.data
    # The other options stay.
    assert entry.options[CONF_FAILURES] == 2
    assert entry.runtime_data is coordinator


def entry_0_1_0(hass, **data) -> MockConfigEntry:
    """An entry as 0.1.0 saved it: minor version 1, the monitoring flag in the data."""
    old = MockConfigEntry(
        domain=DOMAIN,
        title="Driveway",
        unique_id=entry_unique_id(URL, STREAM),
        data={CONF_URL: URL, CONF_STREAM: STREAM, **data},
        options={CONF_FAILURES: 2},
        version=1,
        minor_version=1,
    )
    old.add_to_hass(hass)
    return old


@pytest.mark.parametrize("flag", [False, True])
async def test_migration_moves_the_flag_of_0_1_0_to_the_options(hass, camera, flag):
    old = entry_0_1_0(hass, **{CONF_MONITORING: flag})
    camera.frame(jpeg())
    await setup(hass, old)

    assert old.state is ConfigEntryState.LOADED
    assert old.minor_version == 2
    assert old.data == {CONF_URL: URL, CONF_STREAM: STREAM}
    assert old.options == {CONF_FAILURES: 2, CONF_MONITORING: flag}
    assert state(hass, old, "monitoring") == (STATE_ON if flag else STATE_OFF)
    assert camera.frame_requests == (1 if flag else 0)


async def test_migration_without_the_flag_leaves_the_options(hass, camera):
    old = entry_0_1_0(hass)
    camera.frame(jpeg())
    await setup(hass, old)

    assert old.state is ConfigEntryState.LOADED
    assert old.minor_version == 2
    assert old.data == {CONF_URL: URL, CONF_STREAM: STREAM}
    assert old.options == {CONF_FAILURES: 2}
    assert state(hass, old, "monitoring") == STATE_ON


async def test_migration_runs_once(hass, camera):
    old = entry_0_1_0(hass, **{CONF_MONITORING: False})
    camera.frame(jpeg())
    await setup(hass, old)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": ids(hass, old)["monitoring"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    await hass.config_entries.async_reload(old.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert old.options[CONF_MONITORING] is True
    assert state(hass, old, "monitoring") == STATE_ON

    # Called again on a migrated entry, or on one of a newer minor version, it changes nothing.
    for minor in (2, 3):
        hass.config_entries.async_update_entry(old, minor_version=minor)
        before = (dict(old.data), dict(old.options))
        assert await async_migrate_entry(hass, old) is True
        assert (dict(old.data), dict(old.options)) == before
        assert old.minor_version == minor


async def test_flag_in_the_data_of_a_migrated_entry_is_ignored(hass, camera):
    # 0.1.0 run again on a migrated entry writes the flag back into the data and
    # leaves the minor version at 2. Only the options count, so monitoring stays on.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Driveway",
        unique_id=entry_unique_id(URL, STREAM),
        data={CONF_URL: URL, CONF_STREAM: STREAM, CONF_MONITORING: False},
        options={CONF_FAILURES: 2},
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)
    camera.frame(jpeg())
    await setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, entry, "monitoring") == STATE_ON
    assert camera.frame_requests == 1
    assert entry.minor_version == 2
    assert entry.data == {CONF_URL: URL, CONF_STREAM: STREAM, CONF_MONITORING: False}
    assert entry.options == {CONF_FAILURES: 2}


async def test_entry_of_a_newer_major_version_is_refused(hass, camera):
    new = MockConfigEntry(
        domain=DOMAIN,
        title="Driveway",
        unique_id=entry_unique_id(URL, STREAM),
        data={CONF_URL: URL, CONF_STREAM: STREAM, CONF_MONITORING: False},
        version=2,
        minor_version=1,
    )
    new.add_to_hass(hass)
    assert await async_migrate_entry(hass, new) is False

    await setup(hass, new)
    assert new.state is ConfigEntryState.MIGRATION_ERROR
    assert new.version == 2
    assert new.data[CONF_MONITORING] is False
    assert camera.frame_requests == 0


async def test_turning_on_when_on_changes_nothing(hass, entry, camera):
    camera.frame(b"")
    await setup(hass, entry)
    await tick(hass, entry)
    before = camera.frame_requests

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": ids(hass, entry)["monitoring"]}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert camera.frame_requests == before
    assert state(hass, entry, "connectivity") == STATE_OFF


async def test_reload_in_planned_shutdown_never_shows_unknown(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    entry.runtime_data.async_set_monitoring(False)
    await hass.async_block_till_done(wait_background_tasks=True)
    status = ids(hass, entry)["status"]
    seen: list[str] = []

    def record(event) -> None:
        if event.data["entity_id"] == status and event.data["new_state"] is not None:
            seen.append(event.data["new_state"].state)

    unsubscribe = hass.bus.async_listen("state_changed", record)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    unsubscribe()

    assert STATE_UNKNOWN not in seen
    assert seen[-1] == "disabled"


async def test_frozen_unknown_until_first_frame(hass, entry, camera):
    camera.frame(b"")
    await setup(hass, entry)
    await tick(hass, entry)
    assert state(hass, entry, "connectivity") == STATE_OFF
    assert state(hass, entry, "frozen") == STATE_UNKNOWN

    camera.frame(jpeg())
    await tick(hass, entry)
    assert state(hass, entry, "frozen") == STATE_OFF


async def test_check_across_off_and_on_is_discarded(hass, entry, camera):
    """A failure seen before off-and-on again must not count towards the new threshold."""
    camera.frame(jpeg())
    await setup(hass, entry)
    coordinator = entry.runtime_data
    gate = Gate()
    camera.frame(b"")
    camera.ffmpeg.hook = gate
    stale = hass.async_create_task(coordinator.async_refresh())
    await gate.started.wait()

    coordinator.async_set_monitoring(False)
    coordinator.async_set_monitoring(True)
    gate.release.set()
    await stale
    await hass.async_block_till_done(wait_background_tasks=True)

    # Two failures would reach the threshold of two; only the check after turning on counts.
    assert camera.frame_requests == 3
    assert coordinator.data.failures == 1
    assert state(hass, entry, "connectivity") == STATE_UNKNOWN


async def test_new_frame_replaces_restored_time(hass, entry, camera):
    moment = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    restore_last_frame(hass, entry, moment.isoformat(), datetime_value=True)
    camera.frame(b"")
    await setup(hass, entry)
    assert dt_util.parse_datetime(state(hass, entry, "last_frame")) == moment

    camera.frame(jpeg())
    await tick(hass, entry)
    assert dt_util.parse_datetime(state(hass, entry, "last_frame")) > moment

    newest = state(hass, entry, "last_frame")
    entry.runtime_data.async_set_monitoring(False)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state(hass, entry, "last_frame") == newest


async def test_restored_value_that_is_not_a_time_is_ignored(hass, entry, camera):
    restore_last_frame(hass, entry, "garbage", datetime_value=False)
    camera.frame(b"")
    await setup(hass, entry)
    assert state(hass, entry, "last_frame") == STATE_UNKNOWN


def restore_last_frame(hass, entry, value: str, *, datetime_value: bool) -> None:
    entity_id = "sensor.driveway_last_frame"
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_last_frame",
        suggested_object_id="driveway_last_frame",
        config_entry=entry,
    )
    native = (
        {"__type": "<class 'datetime.datetime'>", "isoformat": value} if datetime_value else value
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(entity_id, value),
                {"native_value": native, "native_unit_of_measurement": None},
            )
        ],
    )


async def test_response_time_is_the_ffmpeg_run(hass, entry, camera, monkeypatch):
    """The time is measured around ffmpeg only: here exactly 250 ms."""
    clock = [1000.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return clock[0]

    async def run() -> None:
        clock[0] += 0.25

    monkeypatch.setattr("custom_components.frame_pulse.probe.time", Clock)
    camera.frame(jpeg())
    camera.ffmpeg.hook = run
    await setup(hass, entry)

    assert state(hass, entry, "response_time") == "250"


async def test_unload_stops_polling(hass, entry, camera):
    camera.frame(jpeg())
    await setup(hass, entry)
    before = camera.frame_requests

    assert await hass.config_entries.async_unload(entry.entry_id)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=10))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert camera.frame_requests == before
    assert state(hass, entry, "status") == STATE_UNAVAILABLE


async def test_unload_cancels_a_hanging_first_check(hass, entry, camera):
    camera.frame(jpeg())
    gate = Gate()
    camera.ffmpeg.hook = gate
    await setup_without_waiting(hass, entry)
    await gate.started.wait()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_hanging_api_does_not_take_a_slot(hass, entry, camera):
    """go2rtc API calls run outside the semaphore: only ffmpeg runs are limited."""
    release = asyncio.Event()
    calls = 0

    async def hanging(method, url, data):
        nonlocal calls
        calls += 1
        await release.wait()
        raise aiohttp.ClientConnectionError

    camera.mock.clear_requests()
    camera.mock.get(API_URL, side_effect=hanging)
    others = [FramePulseCoordinator(hass, e) for e in extra_entries(hass, 3)]
    hung = [asyncio.create_task(c.async_refresh()) for c in others]
    while calls < 3:
        await asyncio.sleep(0)

    semaphore = probe_semaphore(hass)
    assert not semaphore.locked()
    release.set()
    await asyncio.gather(*hung)
