"""The `frame_pulse.snapshot` action on a real Home Assistant."""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant.const import CONF_URL
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.script import Script
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.frame_pulse import snapshot as snapshot_module
from custom_components.frame_pulse.const import (
    CONF_FRAME_TIMEOUT,
    CONF_MONITORING,
    CONF_SNAPSHOT_STREAM,
    CONF_STREAM,
    DOMAIN,
    MAX_CONCURRENT_SNAPSHOTS,
)
from custom_components.frame_pulse.coordinator import entry_unique_id, probe_semaphore
from custom_components.frame_pulse.diagnostics import async_get_config_entry_diagnostics
from custom_components.frame_pulse.probe import Grab
from custom_components.frame_pulse.snapshot import STALE_TEMPORARY_SECONDS

from .conftest import (
    API_INFO,
    API_URL,
    NOT_FOUND,
    RTSP_URL,
    SECRET_URL,
    STREAM,
    STREAMS_URL,
    URL,
    jpeg,
)

MAIN = "driveway_main"
MAIN_RTSP_URL = f"rtsp://go2rtc.test:8554/{MAIN}?video"


@pytest.fixture
def folder(hass, tmp_path) -> Path:
    """A folder the action may write to."""
    allowed = tmp_path.resolve() / "allowed"
    allowed.mkdir()
    hass.config.allowlist_external_dirs = {str(allowed)}
    return allowed


@pytest.fixture
async def loaded(hass, entry, aioclient_mock, ffmpeg) -> MockConfigEntry:
    """The camera entry set up with a working camera; its snapshot stream is the main one."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_SNAPSHOT_STREAM: MAIN}
    )
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    ffmpeg.urls.clear()
    ffmpeg.timeouts.clear()
    return entry


def go2rtc(aioclient_mock, streams=None, **api) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(API_URL, **(api or {"json": API_INFO}))
    aioclient_mock.get(STREAMS_URL, json={STREAM: {}, MAIN: {}} if streams is None else streams)


async def snapshot(hass, entry: MockConfigEntry | None = None, **data) -> dict:
    if entry is not None:
        data.setdefault("config_entry_id", entry.entry_id)
    return await hass.services.async_call(
        DOMAIN, "snapshot", data, blocking=True, return_response=True
    )


def listing(folder: Path) -> list[str]:
    """All names in the folder, hidden temporary files included."""
    return sorted(os.listdir(folder))


async def test_saves_a_frame_of_the_snapshot_stream(hass, loaded, ffmpeg, folder):
    frame = jpeg(7, 4096)
    ffmpeg.outcome = Grab(0, frame)
    status = loaded.runtime_data.data

    response = await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))

    assert response == {"path": str(folder / "shot.jpg"), "size": 4096}
    assert (folder / "shot.jpg").read_bytes() == frame
    assert listing(folder) == ["shot.jpg"]
    assert ffmpeg.urls == [MAIN_RTSP_URL]
    # A snapshot is not a check: the camera state stays as it was.
    assert loaded.runtime_data.data is status


async def test_file_has_the_usual_permissions(hass, loaded, folder):
    umask = os.umask(0o022)
    try:
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    finally:
        os.umask(umask)
    assert (folder / "shot.jpg").stat().st_mode & 0o777 == 0o644


async def test_camera_named_by_its_device(hass, loaded, ffmpeg, folder):
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), loaded.entry_id)
    await snapshot(hass, device_id=device.id, filename=str(folder / "shot.jpg"))
    assert (folder / "shot.jpg").exists()


async def test_monitored_stream_and_frame_timeout_by_default(hass, entry, aioclient_mock, ffmpeg):
    hass.config_entries.async_update_entry(entry, options={CONF_FRAME_TIMEOUT: 12})
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.config.allowlist_external_dirs = {"/"}
    ffmpeg.urls.clear()
    ffmpeg.timeouts.clear()
    ffmpeg.outcome = OSError(2, "stop before the write")

    with pytest.raises(HomeAssistantError):
        await snapshot(hass, entry, filename="/nonexistent-frame-pulse/shot.jpg")
    assert ffmpeg.urls == [RTSP_URL]
    assert ffmpeg.timeouts == [12]


async def test_timeout_of_the_call(hass, loaded, ffmpeg, folder):
    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"), timeout=7)
    assert ffmpeg.timeouts == [7]


async def test_best_quality_by_default_and_none_for_checks(hass, loaded, ffmpeg, folder):
    """A check leaves the quality to ffmpeg; a snapshot fixes it at 2 unless told otherwise."""
    assert ffmpeg.qualities
    assert set(ffmpeg.qualities) == {None}
    ffmpeg.qualities.clear()
    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert ffmpeg.qualities == [2]


@pytest.mark.parametrize("quality", [2, "7", 31])
async def test_quality_of_the_call(hass, loaded, ffmpeg, folder, quality):
    ffmpeg.qualities.clear()
    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"), quality=quality)
    assert ffmpeg.qualities == [int(quality)]


async def test_filename_template_and_missing_folder(hass, loaded, folder):
    response = await snapshot(hass, loaded, filename=str(folder) + "/{{ 'day' }}/{{ 1 + 1 }}.jpg")
    assert response["path"] == str(folder / "day" / "2.jpg")
    assert (folder / "day" / "2.jpg").exists()


async def test_works_while_monitoring_is_off(hass, loaded, ffmpeg, folder):
    loaded.runtime_data.async_set_monitoring(False)
    await hass.async_block_till_done()
    assert loaded.options[CONF_MONITORING] is False

    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert (folder / "shot.jpg").exists()
    assert loaded.runtime_data.data.status == "disabled"


async def test_replaces_an_existing_file(hass, loaded, ffmpeg, folder):
    (folder / "shot.jpg").write_bytes(b"old")
    ffmpeg.outcome = Grab(0, jpeg(3))
    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert (folder / "shot.jpg").read_bytes() == jpeg(3)
    assert listing(folder) == ["shot.jpg"]


async def test_call_without_a_response(hass, loaded, folder):
    await hass.services.async_call(
        DOMAIN,
        "snapshot",
        {"config_entry_id": loaded.entry_id, "filename": str(folder / "shot.jpg")},
        blocking=True,
    )
    assert (folder / "shot.jpg").exists()


@pytest.mark.parametrize(
    ("grab", "streams", "status", "detail"),
    [
        (Grab(None), None, "frame_timeout", "no frame in 20 s"),
        (Grab(0, b""), None, "no_stream", "0 bytes"),
        (Grab(0, b"\xff\xd8\xff not a whole jpeg"), None, "bad_frame", "20 bytes"),
        (Grab(8, b"", NOT_FOUND), {STREAM: {}}, "stream_not_found", "go2rtc has no such stream"),
        (
            Grab(1, b"", "Error opening input files: Connection refused"),
            None,
            "probe_error",
            "ffmpeg 1: Error opening input files: Connection refused",
        ),
        (OSError(2, "No such file or directory"), None, "probe_error", "ffmpeg cannot start"),
    ],
)
async def test_no_file_without_a_valid_frame(
    hass, loaded, aioclient_mock, ffmpeg, folder, grab, streams, status, detail
):
    go2rtc(aioclient_mock, streams=streams)
    ffmpeg.outcome = grab
    (folder / "kept.jpg").write_bytes(b"old")

    with pytest.raises(HomeAssistantError) as raised:
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    with pytest.raises(HomeAssistantError):
        await snapshot(hass, loaded, filename=str(folder / "kept.jpg"))

    assert not isinstance(raised.value, ServiceValidationError)
    assert raised.value.translation_key == "snapshot_failed"
    placeholders = raised.value.translation_placeholders
    assert placeholders["camera"] == "Driveway"
    assert placeholders["status"] == status
    assert placeholders["detail"].startswith(detail)
    assert str(raised.value).startswith(f"No snapshot of Driveway: {status} (")
    assert listing(folder) == ["kept.jpg"]
    assert (folder / "kept.jpg").read_bytes() == b"old"


async def test_go2rtc_unreachable(hass, loaded, aioclient_mock, ffmpeg, folder):
    go2rtc(aioclient_mock, status=500)
    with pytest.raises(HomeAssistantError) as raised:
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert raised.value.translation_placeholders["detail"] == "go2rtc API failed: HTTP 500"
    assert ffmpeg.urls == []
    assert listing(folder) == []


async def test_refused_api_login(hass, loaded, aioclient_mock, ffmpeg, folder):
    go2rtc(aioclient_mock, status=401)
    with pytest.raises(HomeAssistantError) as raised:
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert raised.value.translation_key == "api_auth_failed"
    assert listing(folder) == []
    # A snapshot does not start the login request: the checks do that.
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_secret_stream_name_stays_out_of_the_error(
    hass, entry, aioclient_mock, ffmpeg, folder
):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_SNAPSHOT_STREAM: SECRET_URL}
    )
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    ffmpeg.outcome = Grab(1, b"", f"[rtsp @ 0x1] {SECRET_URL}: Invalid data")

    with pytest.raises(HomeAssistantError) as raised:
        await snapshot(hass, entry, filename=str(folder / "shot.jpg"))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert "hunter2" not in str(raised.value)
    assert "hunter2" not in str(raised.value.translation_placeholders)
    assert diagnostics["options"][CONF_SNAPSHOT_STREAM] == "rtsp://***"
    assert "hunter2" not in str(diagnostics)


async def test_long_file_name(hass, loaded, folder):
    name = "a" * 251 + ".jpg"
    await snapshot(hass, loaded, filename=str(folder / name))
    assert listing(folder) == [name]


async def test_write_failure_leaves_nothing(hass, loaded, folder, monkeypatch):
    def broken_replace(source, target):
        raise IsADirectoryError(21, "Is a directory")

    monkeypatch.setattr("custom_components.frame_pulse.snapshot.os.replace", broken_replace)
    with pytest.raises(HomeAssistantError) as raised:
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert raised.value.translation_key == "snapshot_write_failed"
    assert raised.value.translation_placeholders == {
        "path": str(folder / "shot.jpg"),
        "error": "Is a directory",
    }
    # The temporary file is gone too.
    assert listing(folder) == []


async def test_folder_that_cannot_be_written(hass, loaded, folder):
    (folder / "locked").mkdir()
    (folder / "locked").chmod(0o500)
    try:
        with pytest.raises(HomeAssistantError) as raised:
            await snapshot(hass, loaded, filename=str(folder / "locked" / "shot.jpg"))
    finally:
        (folder / "locked").chmod(0o700)
    assert raised.value.translation_key == "snapshot_write_failed"
    assert raised.value.translation_placeholders["error"] == "Permission denied"
    assert listing(folder / "locked") == []


async def test_write_interrupted_after_the_temporary_file(hass, loaded, folder, monkeypatch):
    def broken_replace(source, target):
        raise KeyboardInterrupt

    monkeypatch.setattr("custom_components.frame_pulse.snapshot.os.replace", broken_replace)
    with pytest.raises(KeyboardInterrupt):
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert listing(folder) == []


@pytest.mark.parametrize(
    ("filename", "key"),
    [
        ("shot.jpg", "path_not_absolute"),
        ("", "path_not_absolute"),
        ("/etc/frame_pulse_shot.jpg", "path_not_allowed"),
        ("{folder}/../outside.jpg", "path_not_allowed"),
        # A forgotten file name: the folder itself, allowed as it is.
        ("{folder}/", "path_is_folder"),
        ("{folder}/missing/", "path_is_folder"),
        ("{folder}/.", "path_is_folder"),
        ("{folder}/sub/..", "path_is_folder"),
        ("{folder}", "path_is_folder"),
    ],
)
async def test_path_checked_before_the_frame(hass, loaded, ffmpeg, folder, filename, key):
    with pytest.raises(ServiceValidationError) as raised:
        await snapshot(hass, loaded, filename=filename.format(folder=folder))
    assert raised.value.translation_key == key
    assert ffmpeg.urls == []


async def test_symlink_out_of_the_allowed_folder(hass, loaded, ffmpeg, folder, tmp_path):
    (folder / "link").symlink_to(tmp_path.resolve())
    with pytest.raises(ServiceValidationError) as raised:
        await snapshot(hass, loaded, filename=str(folder / "link" / "shot.jpg"))
    assert raised.value.translation_key == "path_not_allowed"
    assert ffmpeg.urls == []


async def test_unknown_camera(hass, loaded, ffmpeg, folder):
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("other", "x")}
    )
    calls = [
        {"config_entry_id": "missing"},
        {"config_entry_id": other.entry_id},
        {"device_id": "missing"},
        {"device_id": device.id},
    ]
    for camera in calls:
        with pytest.raises(ServiceValidationError) as raised:
            await snapshot(hass, filename=str(folder / "shot.jpg"), **camera)
        assert raised.value.translation_key == "camera_not_found"
    assert ffmpeg.urls == []


async def test_action_exists_without_cameras(hass, folder):
    """The action is registered with the integration, not with a camera entry."""
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as raised:
        await snapshot(hass, config_entry_id="missing", filename=str(folder / "shot.jpg"))
    assert raised.value.translation_key == "camera_not_found"


async def test_camera_not_loaded(hass, loaded, ffmpeg, folder):
    await hass.config_entries.async_unload(loaded.entry_id)
    with pytest.raises(ServiceValidationError) as raised:
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert raised.value.translation_key == "camera_not_loaded"
    assert raised.value.translation_placeholders == {"camera": "Driveway"}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"config_entry_id": "a", "device_id": "b"},
        {"config_entry_id": "a", "timeout": 4},
        {"config_entry_id": "a", "timeout": 61},
        {"config_entry_id": "a", "quality": 1},
        {"config_entry_id": "a", "quality": 32},
        {"config_entry_id": "a", "quality": "best"},
    ],
)
async def test_invalid_calls(hass, loaded, folder, data):
    with pytest.raises(vol.Invalid):
        await snapshot(hass, filename=str(folder / "shot.jpg"), **data)


async def test_snapshot_does_not_wait_for_check_slots(hass, loaded, folder):
    """Checks of hanging cameras hold the check slots; a snapshot has slots of its own."""
    semaphore = probe_semaphore(hass)
    while not semaphore.locked():
        await semaphore.acquire()
    async with asyncio.timeout(5):
        await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert (folder / "shot.jpg").exists()


async def test_snapshots_have_bounded_parallel_runs(hass, loaded, ffmpeg, folder):
    running = 0
    peak = 0

    async def count() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    ffmpeg.hook = count
    await asyncio.gather(
        *(snapshot(hass, loaded, filename=str(folder / f"{n}.jpg")) for n in range(5))
    )
    assert peak == MAX_CONCURRENT_SNAPSHOTS
    assert len(listing(folder)) == 5


async def test_secret_snapshot_stream_reaches_ffmpeg_whole(hass, aioclient_mock, ffmpeg, folder):
    """The stored stream name is the real one, not its safe label."""
    camera = MockConfigEntry(
        domain=DOMAIN,
        title="Yard",
        unique_id=entry_unique_id(URL, "yard_sub"),
        data={CONF_URL: URL, CONF_STREAM: "yard_sub"},
        options={CONF_SNAPSHOT_STREAM: SECRET_URL},
        minor_version=2,
    )
    camera.add_to_hass(hass)
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(camera.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    ffmpeg.urls.clear()

    await snapshot(hass, camera, filename=str(folder / "shot.jpg"))
    assert ffmpeg.urls == [
        "rtsp://go2rtc.test:8554/rtsp%3A%2F%2Fadmin%3Ahunter2%4010.0.0.5%2Fstream?video"
    ]


async def test_response_is_optional(hass, loaded):
    assert hass.services.supports_response(DOMAIN, "snapshot") is SupportsResponse.OPTIONAL


# The steps of an automation that sends the path of the snapshot on, or an empty one
# when there is none (see the README).
AUTOMATION = [
    {
        "action": "frame_pulse.snapshot",
        "continue_on_error": True,
        "data": {"config_entry_id": "{{ entry_id }}", "filename": "{{ folder }}/{{ snap }}"},
        "response_variable": "shot",
    },
    {
        "event": "snapshot_saved",
        "event_data": {"snap": "{{ snap if shot is defined and shot.size is defined else '' }}"},
    },
]


async def run_automation(hass, entry, folder) -> str:
    """Run the automation steps through the script engine; return the `snap` it sent."""
    saved = async_capture_events(hass, "snapshot_saved")
    script = Script(hass, cv.SCRIPT_SCHEMA(AUTOMATION), "Snapshot test", DOMAIN)
    await script.async_run(
        {"entry_id": entry.entry_id, "folder": str(folder), "snap": "push/shot.jpg"},
        Context(),
    )
    await hass.async_block_till_done()
    assert len(saved) == 1
    return saved[0].data["snap"]


async def test_automation_gets_the_snapshot(hass, loaded, folder):
    assert await run_automation(hass, loaded, folder) == "push/shot.jpg"
    assert (folder / "push" / "shot.jpg").exists()


@pytest.mark.parametrize(
    "outcome",
    [Grab(None), Grab(0, b""), Grab(1, b"", "Connection refused"), OSError(2, "No ffmpeg")],
)
async def test_automation_goes_on_without_a_frame(hass, loaded, ffmpeg, folder, outcome):
    ffmpeg.outcome = outcome
    assert await run_automation(hass, loaded, folder) == ""
    assert listing(folder) == []


async def test_automation_goes_on_without_go2rtc(hass, loaded, aioclient_mock, ffmpeg, folder):
    ffmpeg.outcome = Grab(None)
    go2rtc(aioclient_mock, status=500)
    assert await run_automation(hass, loaded, folder) == ""


async def test_automation_goes_on_when_the_file_cannot_be_written(hass, loaded, folder):
    (folder / "push").mkdir()
    (folder / "push").chmod(0o500)
    try:
        assert await run_automation(hass, loaded, folder) == ""
    finally:
        (folder / "push").chmod(0o700)


async def test_automation_goes_on_while_the_camera_is_not_loaded(hass, loaded, folder):
    await hass.config_entries.async_unload(loaded.entry_id)
    assert await run_automation(hass, loaded, folder) == ""


async def test_entry_reloaded_during_the_snapshot(hass, loaded, folder, monkeypatch):
    """Options saved or Home Assistant stopping while the path is checked: the frame is still saved.

    An unloaded entry has no runtime data; reading it then would raise an error that
    `continue_on_error` does not catch, and the automation would stop.
    """
    started = asyncio.Event()
    release = threading.Event()
    check = snapshot_module._path_problem

    def held_check(hass, path):
        hass.loop.call_soon_threadsafe(started.set)
        release.wait(5)
        return check(hass, path)

    monkeypatch.setattr(snapshot_module, "_path_problem", held_check)
    call = hass.async_create_task(snapshot(hass, loaded, filename=str(folder / "shot.jpg")))
    await started.wait()
    await hass.config_entries.async_unload(loaded.entry_id)
    assert not hasattr(loaded, "runtime_data")
    release.set()
    assert (await call)["path"] == str(folder / "shot.jpg")
    assert (folder / "shot.jpg").exists()


async def test_stale_temporary_files_are_removed(hass, loaded, folder):
    """A write cut short (Home Assistant killed) leaves a temporary file; a later one removes it."""
    stale = folder / ".frame_pulse-0123456789abcdef.tmp"
    fresh = folder / ".frame_pulse-fedcba9876543210.tmp"
    other = folder / ".other.tmp"
    for path in (stale, fresh, other):
        path.write_bytes(b"half")
    old = stale.stat().st_mtime - STALE_TEMPORARY_SECONDS - 5
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))

    # The fresh one may belong to a write going on right now; files of others stay.
    assert listing(folder) == [fresh.name, other.name, "shot.jpg"]


async def test_temporary_file_is_hidden_from_media(hass, loaded, folder, monkeypatch):
    """Media lists no names starting with a dot, and the temporary file has one."""
    seen = []
    replace = os.replace

    def watch(source, target):
        seen.append(os.path.basename(source))
        replace(source, target)

    monkeypatch.setattr("custom_components.frame_pulse.snapshot.os.replace", watch)
    await snapshot(hass, loaded, filename=str(folder / "shot.jpg"))
    assert len(seen) == 1
    assert seen[0].startswith(".")
