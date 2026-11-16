"""UI setup."""

from __future__ import annotations

import aiohttp
import probatio
import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.data_entry_flow import FlowResultType, InvalidData, section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.frame_pulse.config_flow import _clean_url
from custom_components.frame_pulse.const import (
    CONF_API,
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_FAILURES,
    CONF_FRAME_TIMEOUT,
    CONF_MONITORING,
    CONF_RTSP,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_SAME_FRAMES,
    CONF_STREAM,
    DEFAULT_SAME_FRAMES,
    DEFAULT_URL,
    DOMAIN,
)
from custom_components.frame_pulse.coordinator import entry_unique_id
from custom_components.frame_pulse.probe import MAX_STREAMS_BYTES, Grab

from .conftest import API_INFO, API_URL, NOT_FOUND, SECRET_URL, STREAM, STREAMS_URL, URL

STREAMS = {STREAM: {"producers": [{"url": SECRET_URL}]}, "garage_sub": {"producers": []}}

pytestmark = pytest.mark.usefixtures("ffmpeg")


def go2rtc(aioclient_mock, streams=STREAMS, **api) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(API_URL, **(api or {"json": API_INFO}))
    aioclient_mock.get(STREAMS_URL, json=streams)


async def to_stream_step(hass, aioclient_mock):
    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{URL}/", CONF_API: {}}
    )


def suggestions(schema) -> dict:
    """Map each form field, also inside sections, to its suggested value."""
    result = {}
    for key, value in schema.schema.items():
        if isinstance(value, section):
            result[str(key)] = suggestions(value.schema)
        else:
            result[str(key)] = (key.description or {}).get("suggested_value")
    return result


def browser_data(schema) -> dict:
    """Return what the browser fills a form with and sends if nothing is typed.

    The schema is serialized as for the frontend, then filled the way the
    frontend's computeInitialHaFormData does: a field's suggested value, else
    its default, else for a section its own fields, kept if required or not
    empty. Before the send, empty strings and unset values are dropped.
    """

    def fill(fields: list[dict]) -> dict:
        data = {}
        for field in fields:
            suggested = (field.get("description") or {}).get("suggested_value")
            if suggested is not None:
                data[field["name"]] = suggested
            elif "default" in field:
                data[field["name"]] = field["default"]
            elif field.get("type") == "expandable":
                inner = fill(field["schema"])
                if field.get("required") or inner:
                    data[field["name"]] = inner
        return data

    serialized = probatio.to_field_list(schema, custom_serializer=cv.custom_serializer)
    return {key: value for key, value in fill(serialized).items() if value not in ("", None)}


async def test_create_with_working_frame(hass, aioclient_mock):
    result = await to_stream_step(hass, aioclient_mock)
    assert result["step_id"] == "stream"
    options = result["data_schema"].schema[CONF_STREAM].config["options"]
    assert options == [
        {"value": STREAM, "label": STREAM},
        {"value": "garage_sub", "label": "garage_sub"},
    ]
    assert "hunter2" not in str(result)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Driveway"
    assert result["data"] == {CONF_URL: URL, CONF_STREAM: STREAM}


async def test_failed_frame_does_not_block_saving(hass, aioclient_mock, ffmpeg):
    ffmpeg.outcome = Grab(0, b"")
    result = await to_stream_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_stream_not_found_shows_error(hass, aioclient_mock, ffmpeg):
    """The stream was removed from go2rtc after the list was shown."""
    ffmpeg.outcome = Grab(8, b"", NOT_FOUND)
    result = await to_stream_step(hass, aioclient_mock)
    go2rtc(aioclient_mock, streams={})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["errors"] == {CONF_STREAM: "stream_not_found"}


async def test_go2rtc_gone_at_trial_shows_error(hass, aioclient_mock):
    result = await to_stream_step(hass, aioclient_mock)
    go2rtc(aioclient_mock, exc=aiohttp.ClientConnectionError())
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["errors"] == {"base": "check_failed"}
    assert result["description_placeholders"] == {"reason": "go2rtc API failed: no response"}


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (
            Grab(195, b"", "Error opening input files: Connection refused"),
            "ffmpeg 195: Error opening input files: Connection refused",
        ),
        (FileNotFoundError(2, "No such file or directory"), "ffmpeg cannot start: No such file"),
    ],
    ids=["rtsp-refused", "no-ffmpeg"],
)
async def test_broken_rtsp_at_trial_names_the_reason(hass, aioclient_mock, ffmpeg, outcome, reason):
    """The API answered a step ago: the form names the RTSP or ffmpeg reason instead."""
    result = await to_stream_step(hass, aioclient_mock)
    ffmpeg.outcome = outcome
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["errors"] == {"base": "check_failed"}
    assert result["description_placeholders"]["reason"].startswith(reason)

    ffmpeg.outcome = Grab(0, b"")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_large_stream_list_is_accepted(hass, aioclient_mock):
    """A big setup lists every stream with its sources: megabytes, unlike `/api`."""
    streams = {f"cam{n:03}": {"producers": [{"url": "x" * 20_000}]} for n in range(100)}
    go2rtc(aioclient_mock, streams=streams)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["step_id"] == "stream"


async def test_endless_stream_list_is_rejected(hass, aioclient_mock):
    go2rtc(aioclient_mock, streams={"cam": "x" * (MAX_STREAMS_BYTES + 1)})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "reply",
    [
        {"exc": aiohttp.ClientConnectionError()},
        {"exc": TimeoutError()},
        {"status": 403},
        {"status": 500},
        {"text": "<html>not go2rtc</html>"},
        {"json": []},
    ],
    ids=["refused", "timeout", "403", "500", "not_json", "json_list"],
)
async def test_go2rtc_unreachable_then_corrected(hass, aioclient_mock, reply):
    aioclient_mock.get(STREAMS_URL, **reply)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["step_id"] == "stream"


async def test_no_streams(hass, aioclient_mock):
    aioclient_mock.get(STREAMS_URL, json={})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["errors"] == {"base": "no_streams"}


async def test_duplicate_stream_aborts(hass, entry, aioclient_mock):
    result = await to_stream_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Duplicate", CONF_RTSP: {}}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_applied_without_restart(hass, entry, aioclient_mock, ffmpeg):
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 120,
            CONF_FRAME_TIMEOUT: 10,
            CONF_FAILURES: 3,
            CONF_SAME_FRAMES: 0,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    coordinator = entry.runtime_data
    assert coordinator.update_interval.total_seconds() == 120
    assert ffmpeg.timeouts[-1] == 10

    # same_frames 0: identical frames never freeze, also past the default threshold.
    for _ in range(DEFAULT_SAME_FRAMES + 1):
        await coordinator.async_refresh()
    assert coordinator.data.status == "ok"

    # failures 3: the connection goes off on the third failure, not the second.
    ffmpeg.outcome = Grab(0, b"")
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.data.connected is True
    await coordinator.async_refresh()
    assert coordinator.data.connected is False


async def test_same_frames_one_is_rejected(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 60,
            CONF_FRAME_TIMEOUT: 20,
            CONF_FAILURES: 2,
            CONF_SAME_FRAMES: 1,
        },
    )
    assert result["errors"] == {CONF_SAME_FRAMES: "invalid_same_frames"}


async def test_reconfigure_saves_wrong_port_without_check(hass, entry, aioclient_mock):
    """A wrong port is saved: the failure shows up in the status, not in the form."""
    go2rtc(aioclient_mock)
    wrong = "http://other.test:1985"  # the mock compares the host only, not the port
    aioclient_mock.get(f"{wrong}/api", exc=aiohttp.ClientConnectionError())
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: wrong, CONF_API: {}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == wrong
    assert entry.unique_id == f"other.test:1985|{STREAM}"
    assert entry.runtime_data.data.status == "probe_error"


async def test_streams_named_by_address_are_masked_and_distinct(hass, aioclient_mock, ffmpeg):
    """The raw name reaches neither the label nor the option value sent to the browser."""
    other = "rtsp://admin:hunter2@10.0.0.6/stream2"
    go2rtc(aioclient_mock, streams={SECRET_URL: {}, other: {}, STREAM: {}, "ring:?token=x": {}})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )

    options = result["data_schema"].schema[CONF_STREAM].config["options"]
    labels = [option["label"] for option in options]
    assert "hunter2" not in str(options)
    assert len(set(labels)) == len(labels)
    assert STREAM in labels

    (choice,) = [o["value"] for o in options if o["label"] == "rtsp://*** (2)"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: choice, CONF_NAME: "Yard", CONF_RTSP: {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_STREAM] == other
    assert "10.0.0.6" in ffmpeg.urls[-1]  # the chosen stream, not the other address


async def test_raw_stream_name_not_suggested_after_error(hass, aioclient_mock, ffmpeg):
    go2rtc(aioclient_mock, streams={SECRET_URL: {}})
    ffmpeg.outcome = Grab(8, b"", NOT_FOUND)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    (option,) = result["data_schema"].schema[CONF_STREAM].config["options"]
    go2rtc(aioclient_mock, streams={})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: option["value"], CONF_NAME: "Yard", CONF_RTSP: {}}
    )
    assert result["errors"] == {CONF_STREAM: "stream_not_found"}
    suggested = {
        str(key): (key.description or {}).get("suggested_value")
        for key in result["data_schema"].schema
    }
    assert suggested[CONF_STREAM] == "rtsp://***"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" http://host:1984/ ", "http://host:1984"),
        ("http://host:1984/streams.html?src=cam", "http://host:1984"),
        ("http://host:1984/#/streams", "http://host:1984"),
        ("https://proxy.test/go2rtc/", "https://proxy.test/go2rtc"),
        ("https://proxy.test/go2rtc/index.html", "https://proxy.test/go2rtc"),
        ("host:1984", None),
        ("not a url", None),
        ("ftp://host:1984", None),
        ("ftp://admin:hunter2@host:1984", None),
        # An encoded slash belongs to the proxy path: it is not turned into a real one.
        ("https://proxy.test/a%2Fb/", "https://proxy.test/a%2Fb"),
        ("https://proxy.test/a%2Fb/streams.html", "https://proxy.test/a%2Fb"),
        # An API address copied from the browser.
        ("http://host:1984/api", "http://host:1984"),
        ("http://host:1984/api/", "http://host:1984"),
        ("http://host:1984/api/streams?src=cam", "http://host:1984"),
        ("https://proxy.test/go2rtc/api/frame.jpeg?src=cam", "https://proxy.test/go2rtc"),
        # A longer path after `api` is a proxy path, and so is a segment that only contains it.
        ("https://proxy.test/api/ingress/abc", "https://proxy.test/api/ingress/abc"),
        ("https://proxy.test/myapi", "https://proxy.test/myapi"),
    ],
)
def test_clean_url(raw, expected):
    assert _clean_url(raw) == (expected, None if expected else "invalid_url")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://admin:hunter2@host:1984", "http://host:1984"),
        ("http://admin@host:1984/", "http://host:1984"),
        ("http://:hunter2@host:1984", "http://host:1984"),
        ("https://admin:hunter2@proxy.test/go2rtc/streams.html", "https://proxy.test/go2rtc"),
    ],
    ids=["user-and-password", "user-only", "password-only", "proxy-path"],
)
def test_clean_url_refuses_a_login_and_returns_the_address_without_it(raw, expected):
    assert _clean_url(raw) == (expected, "url_login")


async def test_same_stream_on_another_go2rtc_is_another_camera(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)  # the existing entry is checked when the new one sets up
    other = "http://other.test:1984"
    aioclient_mock.get(f"{other}/api", json=API_INFO)
    aioclient_mock.get(f"{other}/api/streams", json=STREAMS)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: other, CONF_API: {}}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Other driveway", CONF_RTSP: {}}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reconfigure_onto_monitored_stream_aborts(hass, entry, aioclient_mock):
    other = "http://other.test:1984"
    second = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"other.test:1984|{STREAM}",
        data={CONF_URL: other, CONF_STREAM: STREAM},
    )
    second.add_to_hass(hass)

    result = await second.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}, CONF_RTSP: {}}
    )

    assert result["reason"] == "already_configured"
    assert second.data[CONF_URL] == other


async def test_invalid_url_can_be_corrected(hass, aioclient_mock):
    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://go2rtc.test:1984", CONF_API: {}}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    assert aioclient_mock.call_count == 0

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["step_id"] == "stream"


async def test_reconfigure_invalid_url_can_be_corrected(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "go2rtc", CONF_API: {}, CONF_RTSP: {}}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    # What was typed comes back to be corrected: it holds no login.
    assert browser_data(result["data_schema"])[CONF_URL] == "go2rtc"

    other = "http://other.test:1984"
    aioclient_mock.get(f"{other}/api", json=API_INFO)
    aioclient_mock.get(f"{other}/api/streams", json=STREAMS)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: other, CONF_API: {}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == other


@pytest.mark.parametrize("own", [URL, f"{URL}/", f"{URL}/streams.html"])
async def test_reconfigure_onto_own_address_succeeds(hass, entry, aioclient_mock, own):
    go2rtc(aioclient_mock)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: own, CONF_API: {}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == URL


async def test_reconfigure_keeps_planned_shutdown_and_entities(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MONITORING: False})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}

    result = await entry.start_reconfigure_flow(hass)
    other = "http://other.test:1984"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: other, CONF_API: {}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_URL: other, CONF_STREAM: STREAM, CONF_MONITORING: False}
    assert entry.runtime_data.data.status == "disabled"
    after = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert after == before


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://go2rtc.test:1984", f"go2rtc.test:1984|{STREAM}"),
        ("http://go2rtc.test", f"go2rtc.test:80|{STREAM}"),
        ("https://proxy.test/go2rtc", f"proxy.test:443/go2rtc|{STREAM}"),
        ("http://admin:hunter2@go2rtc.test:1984", f"go2rtc.test:1984|{STREAM}"),
        ("https://proxy.test/a%2Fb", f"proxy.test:443/a%2Fb|{STREAM}"),
    ],
)
def test_entry_unique_id(url, expected):
    assert entry_unique_id(url, STREAM) == expected


@pytest.mark.parametrize(
    "stream", [SECRET_URL, "ring:?token=abc123", "cam?password=hunter2", "exec:ffmpeg -i x"]
)
def test_entry_unique_id_hides_a_secret_bearing_stream_name(stream):
    unique_id = entry_unique_id(URL, stream)
    assert unique_id.startswith("go2rtc.test:1984|sha256:")
    assert stream not in unique_id
    assert unique_id != entry_unique_id(URL, stream + "x")


async def test_create_with_rtsp_port_and_login(hass, aioclient_mock, ffmpeg):
    result = await to_stream_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_STREAM: STREAM,
            CONF_NAME: "Driveway",
            CONF_RTSP: {CONF_PORT: 18554.0, CONF_USERNAME: "viewer", CONF_PASSWORD: "pw:1"},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_RTSP_PORT: 18554,
        CONF_RTSP_USERNAME: "viewer",
        CONF_RTSP_PASSWORD: "pw:1",
    }
    assert type(result["data"][CONF_RTSP_PORT]) is int
    # The trial check; the first check of the new entry follows with the same address.
    assert ffmpeg.urls[0] == f"rtsp://viewer:pw%3A1@go2rtc.test:18554/{STREAM}?video"


async def test_wrong_rtsp_login_at_trial_can_be_corrected(hass, aioclient_mock, ffmpeg):
    """The form keeps the port and the username, never the password."""
    result = await to_stream_step(hass, aioclient_mock)
    ffmpeg.outcome = Grab(1, b"", "method DESCRIBE failed: 401 Unauthorized")
    rtsp = {CONF_PORT: 18554, CONF_USERNAME: "viewer", CONF_PASSWORD: "wrong-pw"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert result["errors"] == {"base": "check_failed"}
    assert "401 Unauthorized" in result["description_placeholders"]["reason"]
    assert suggestions(result["data_schema"])[CONF_RTSP] == {
        CONF_PORT: 18554,
        CONF_USERNAME: "viewer",
        CONF_PASSWORD: None,
    }
    assert "wrong-pw" not in str(result)

    ffmpeg.outcome = Grab(0, b"")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {**rtsp, CONF_PASSWORD: "pw"}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_RTSP_PASSWORD] == "pw"


@pytest.mark.parametrize(
    "rtsp",
    [{CONF_USERNAME: "viewer"}, {CONF_PASSWORD: "pw"}, {CONF_USERNAME: "", CONF_PASSWORD: "pw"}],
    ids=["username-only", "password-only", "empty-username"],
)
async def test_half_rtsp_login_is_refused_without_a_check(hass, aioclient_mock, ffmpeg, rtsp):
    result = await to_stream_step(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert result["errors"] == {"base": "rtsp_login"}
    assert ffmpeg.urls == []


@pytest.fixture
def login_entry(hass) -> MockConfigEntry:
    """An entry with an RTSP port and login, monitoring off."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gate",
        unique_id=entry_unique_id(URL, STREAM),
        data={
            CONF_URL: URL,
            CONF_STREAM: STREAM,
            CONF_MONITORING: False,
            CONF_RTSP_PORT: 18554,
            CONF_RTSP_USERNAME: "viewer",
            CONF_RTSP_PASSWORD: "hunter2",
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def test_reconfigure_shows_rtsp_settings_without_the_password(hass, login_entry):
    result = await login_entry.start_reconfigure_flow(hass)
    assert suggestions(result["data_schema"])[CONF_RTSP] == {
        CONF_PORT: 18554,
        CONF_USERNAME: "viewer",
        CONF_PASSWORD: None,
    }
    assert "hunter2" not in str(result)


@pytest.mark.parametrize(
    ("rtsp", "expected"),
    [
        # An empty password keeps the saved one while the username stays.
        (
            {CONF_PORT: 18555, CONF_USERNAME: "viewer"},
            {CONF_RTSP_PORT: 18555, CONF_RTSP_USERNAME: "viewer", CONF_RTSP_PASSWORD: "hunter2"},
        ),
        (
            {CONF_USERNAME: "viewer", CONF_PASSWORD: "new-pw"},
            {CONF_RTSP_USERNAME: "viewer", CONF_RTSP_PASSWORD: "new-pw"},
        ),
        # An empty username drops the login, an empty port drops the port.
        ({}, {}),
        ({CONF_USERNAME: "", CONF_PASSWORD: ""}, {}),
    ],
    ids=["keep-password", "new-password", "drop-all", "cleared"],
)
async def test_reconfigure_saves_rtsp_settings(hass, login_entry, aioclient_mock, rtsp, expected):
    result = await login_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}, CONF_RTSP: rtsp}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert login_entry.data == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_MONITORING: False,
        **expected,
    }


async def test_reconfigure_new_username_needs_a_password(hass, login_entry):
    """The saved password belongs to the saved username only."""
    result = await login_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}, CONF_RTSP: {CONF_USERNAME: "admin"}}
    )
    assert result["errors"] == {"base": "rtsp_login"}
    assert suggestions(result["data_schema"])[CONF_RTSP] == {
        CONF_PORT: None,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: None,
    }
    assert login_entry.data[CONF_RTSP_USERNAME] == "viewer"


async def test_reconfigure_reports_the_url_before_the_login(hass, login_entry):
    result = await login_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "go2rtc", CONF_API: {}, CONF_RTSP: {CONF_PASSWORD: "pw"}}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_reconfigure_in_the_browser_keeps_the_rtsp_settings(hass, login_entry):
    """Saving the form as the browser fills it changes nothing.

    The section has no default of its own: the frontend would take it as the
    whole section and leave the saved port and username out of the form.
    """
    result = await login_entry.start_reconfigure_flow(hass)
    shown = browser_data(result["data_schema"])
    assert shown == {
        CONF_URL: URL,
        CONF_API: {},
        CONF_RTSP: {CONF_PORT: 18554, CONF_USERNAME: "viewer"},
    }

    result = await hass.config_entries.flow.async_configure(result["flow_id"], shown)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert login_entry.data[CONF_RTSP_PORT] == 18554
    assert login_entry.data[CONF_RTSP_USERNAME] == "viewer"
    assert login_entry.data[CONF_RTSP_PASSWORD] == "hunter2"


async def test_stream_form_in_the_browser_shows_typed_rtsp_settings(hass, aioclient_mock, ffmpeg):
    result = await to_stream_step(hass, aioclient_mock)
    assert browser_data(result["data_schema"]) == {CONF_RTSP: {}}

    ffmpeg.outcome = Grab(1, b"", "method DESCRIBE failed: 401 Unauthorized")
    rtsp = {CONF_PORT: 18554, CONF_USERNAME: "viewer", CONF_PASSWORD: "wrong-pw"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert browser_data(result["data_schema"])[CONF_RTSP] == {
        CONF_PORT: 18554,
        CONF_USERNAME: "viewer",
    }


async def test_reconfigure_error_keeps_the_typed_url(hass, login_entry):
    other = "http://other.test:1984"
    result = await login_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: other, CONF_API: {}, CONF_RTSP: {CONF_USERNAME: "admin"}}
    )
    assert result["errors"] == {"base": "rtsp_login"}
    assert browser_data(result["data_schema"])[CONF_URL] == other
    assert login_entry.data[CONF_URL] == URL


# ffmpeg cuts an encoded login longer than 127 characters: "viewer:" is 7.
LONGEST_PASSWORD = "p" * 120


@pytest.mark.parametrize(
    "password",
    [LONGEST_PASSWORD + "p", "!" * 41, "пароль" * 4],
    ids=["one-too-many", "symbols", "cyrillic"],
)
async def test_too_long_rtsp_login_is_refused_without_a_check(
    hass, aioclient_mock, ffmpeg, password
):
    result = await to_stream_step(hass, aioclient_mock)
    rtsp = {CONF_USERNAME: "viewer", CONF_PASSWORD: password}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert result["errors"] == {"base": "rtsp_login_long"}
    assert ffmpeg.urls == []
    assert password not in str(result)


async def test_longest_rtsp_login_reaches_ffmpeg_whole(hass, aioclient_mock, ffmpeg):
    result = await to_stream_step(hass, aioclient_mock)
    rtsp = {CONF_USERNAME: "viewer", CONF_PASSWORD: LONGEST_PASSWORD}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login = ffmpeg.urls[0].removeprefix("rtsp://").partition("@")[0]
    assert login == f"viewer:{LONGEST_PASSWORD}"
    assert len(login) == 127


async def test_reconfigure_refuses_a_too_long_rtsp_login(hass, login_entry):
    result = await login_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: URL,
            CONF_API: {},
            CONF_RTSP: {CONF_USERNAME: "viewer", CONF_PASSWORD: "%" * 41},
        },
    )
    assert result["errors"] == {"base": "rtsp_login_long"}
    assert login_entry.data[CONF_RTSP_PASSWORD] == "hunter2"


API_LOGIN = {CONF_USERNAME: "viewer", CONF_PASSWORD: "hunter2"}
# Base64 of "viewer:hunter2".
VIEWER_HEADER = {"Authorization": "Basic dmlld2VyOmh1bnRlcjI="}
# Base64 of "admin:", the login of go2rtc set up with `api.username` alone.
ADMIN_HEADER = {"Authorization": "Basic YWRtaW46"}


def sent_headers(aioclient_mock) -> list[dict]:
    return [call[3] or {} for call in aioclient_mock.mock_calls]


async def start_user_flow(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_user_form_in_the_browser_has_an_empty_api_section(hass):
    """The section has no default of its own, so the browser sends it empty."""
    result = await start_user_flow(hass)
    assert browser_data(result["data_schema"]) == {CONF_URL: DEFAULT_URL, CONF_API: {}}


async def test_create_with_api_login(hass, aioclient_mock, ffmpeg):
    go2rtc(aioclient_mock)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: API_LOGIN}
    )
    assert result["step_id"] == "stream"
    assert "hunter2" not in str(result)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_API_USERNAME: "viewer",
        CONF_API_PASSWORD: "hunter2",
    }
    assert result["result"].unique_id == f"go2rtc.test:1984|{STREAM}"
    # The stream list, the trial check and the first check of the new entry.
    paths = [call[1].path for call in aioclient_mock.mock_calls]
    assert paths[:2] == ["/api/streams", "/api"]
    assert len(paths) >= 3
    assert sent_headers(aioclient_mock) == [VIEWER_HEADER] * len(paths)
    # The login goes in a header only, never into the ffmpeg address.
    assert all("viewer" not in url and "hunter2" not in url for url in ffmpeg.urls)


async def test_api_login_asked_by_go2rtc_then_given(hass, aioclient_mock):
    aioclient_mock.get(STREAMS_URL, status=401)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {}}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert sent_headers(aioclient_mock) == [{}]

    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: API_LOGIN}
    )
    assert result["step_id"] == "stream"
    assert sent_headers(aioclient_mock) == [VIEWER_HEADER]


async def test_wrong_api_login_keeps_the_username_not_the_password(hass, aioclient_mock):
    aioclient_mock.get(STREAMS_URL, status=401)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{URL}/", CONF_API: API_LOGIN}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    assert suggestions(result["data_schema"])[CONF_API] == {
        CONF_USERNAME: "viewer",
        CONF_PASSWORD: None,
    }
    assert browser_data(result["data_schema"]) == {
        CONF_URL: URL,
        CONF_API: {CONF_USERNAME: "viewer"},
    }
    assert "hunter2" not in str(result)


@pytest.mark.parametrize(
    "api",
    [
        {CONF_PASSWORD: "hunter2"},
        {CONF_USERNAME: "", CONF_PASSWORD: "hunter2"},
        {CONF_USERNAME: "vi:ewer", CONF_PASSWORD: "hunter2"},
    ],
    ids=["password-only", "empty-username", "colon-in-username"],
)
async def test_bad_api_login_is_refused_without_a_request(hass, aioclient_mock, api):
    go2rtc(aioclient_mock)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: api}
    )
    assert result["errors"] == {"base": "api_login"}
    assert aioclient_mock.call_count == 0
    assert "hunter2" not in str(result)


@pytest.mark.parametrize(
    "api", [{CONF_USERNAME: "admin"}, {CONF_USERNAME: "admin", CONF_PASSWORD: ""}]
)
async def test_create_with_a_username_only_api_login(hass, aioclient_mock, api):
    """go2rtc turns its login on with `api.username` alone and takes an empty password."""
    go2rtc(aioclient_mock)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: api}
    )
    assert result["step_id"] == "stream"
    assert sent_headers(aioclient_mock) == [ADMIN_HEADER]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_API_USERNAME: "admin",
        CONF_API_PASSWORD: "",
    }
    assert result["result"].unique_id == f"go2rtc.test:1984|{STREAM}"
    assert all(headers == ADMIN_HEADER for headers in sent_headers(aioclient_mock))


async def test_url_error_comes_before_the_api_login_error(hass, aioclient_mock):
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "go2rtc", CONF_API: {CONF_USERNAME: "viewer"}}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    assert aioclient_mock.call_count == 0


async def test_url_with_a_login_is_refused_and_shown_without_it(hass, aioclient_mock):
    go2rtc(aioclient_mock)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "http://admin:typed-pw@go2rtc.test:1984/", CONF_API: {}}
    )
    assert result["errors"] == {CONF_URL: "url_login"}
    assert aioclient_mock.call_count == 0
    assert browser_data(result["data_schema"])[CONF_URL] == URL
    assert "typed-pw" not in str(result)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: API_LOGIN}
    )
    assert result["step_id"] == "stream"


async def test_invalid_url_is_not_shown_back(hass, aioclient_mock):
    """Even an address that is not valid may carry a password."""
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "ftp://admin:typed-pw@go2rtc.test", CONF_API: {}}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    assert browser_data(result["data_schema"])[CONF_URL] == DEFAULT_URL
    assert "typed-pw" not in str(result)


async def test_api_login_refused_at_trial_names_the_reason(hass, aioclient_mock):
    """The login stopped working between the steps: the trial check says why."""
    go2rtc(aioclient_mock)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: API_LOGIN}
    )
    aioclient_mock.clear_requests()
    aioclient_mock.get(API_URL, status=401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    assert result["errors"] == {"base": "check_failed"}
    assert result["description_placeholders"] == {
        "reason": "go2rtc API failed: HTTP 401: the API login is missing or wrong"
    }
    assert sent_headers(aioclient_mock) == [VIEWER_HEADER]
    assert "hunter2" not in str(result)


@pytest.fixture
def full_entry(hass) -> MockConfigEntry:
    """An entry with an API login and RTSP settings, monitoring off."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gate",
        unique_id=entry_unique_id(URL, STREAM),
        data={
            CONF_URL: URL,
            CONF_STREAM: STREAM,
            CONF_MONITORING: False,
            CONF_API_USERNAME: "viewer",
            CONF_API_PASSWORD: "hunter2",
            CONF_RTSP_PORT: 18554,
            CONF_RTSP_USERNAME: "rtsp-user",
            CONF_RTSP_PASSWORD: "rtsp-pw",
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


RTSP_SAVED = {CONF_RTSP_PORT: 18554, CONF_RTSP_USERNAME: "rtsp-user", CONF_RTSP_PASSWORD: "rtsp-pw"}
RTSP_SHOWN = {CONF_PORT: 18554, CONF_USERNAME: "rtsp-user"}


async def test_reconfigure_shows_the_api_username_without_the_password(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    assert suggestions(result["data_schema"])[CONF_API] == {
        CONF_USERNAME: "viewer",
        CONF_PASSWORD: None,
    }
    assert list(result["data_schema"].schema) == [CONF_URL, CONF_API, CONF_RTSP]
    assert "hunter2" not in str(result)


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        # An empty password keeps the saved one while the username stays.
        ({CONF_USERNAME: "viewer"}, {CONF_API_USERNAME: "viewer", CONF_API_PASSWORD: "hunter2"}),
        (
            {CONF_USERNAME: "viewer", CONF_PASSWORD: "new-pw"},
            {CONF_API_USERNAME: "viewer", CONF_API_PASSWORD: "new-pw"},
        ),
        (
            {CONF_USERNAME: "admin", CONF_PASSWORD: "new-pw"},
            {CONF_API_USERNAME: "admin", CONF_API_PASSWORD: "new-pw"},
        ),
        # An empty username drops the login.
        ({}, {}),
        ({CONF_USERNAME: "", CONF_PASSWORD: ""}, {}),
    ],
    ids=["keep-password", "new-password", "new-login", "drop", "cleared"],
)
async def test_reconfigure_saves_the_api_login_and_keeps_rtsp(hass, full_entry, api, expected):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: api, CONF_RTSP: RTSP_SHOWN}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert full_entry.data == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_MONITORING: False,
        **expected,
        **RTSP_SAVED,
    }


async def test_reconfigure_rtsp_change_keeps_the_api_login(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {CONF_USERNAME: "viewer"}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert full_entry.data == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_MONITORING: False,
        CONF_API_USERNAME: "viewer",
        CONF_API_PASSWORD: "hunter2",
    }


async def test_reconfigure_adds_an_api_login(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: API_LOGIN, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_API_USERNAME: "viewer",
        CONF_API_PASSWORD: "hunter2",
    }
    assert entry.unique_id == f"go2rtc.test:1984|{STREAM}"
    # The reloaded entry checks with the new login.
    assert sent_headers(aioclient_mock)
    assert all(headers == VIEWER_HEADER for headers in sent_headers(aioclient_mock))


async def test_reconfigure_new_api_username_needs_a_password(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {CONF_USERNAME: "admin"}, CONF_RTSP: {}}
    )
    assert result["errors"] == {"base": "api_login"}
    assert suggestions(result["data_schema"])[CONF_API] == {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: None,
    }
    assert full_entry.data[CONF_API_USERNAME] == "viewer"


async def test_reconfigure_error_does_not_show_the_typed_api_password(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: URL,
            CONF_API: {CONF_USERNAME: "admin", CONF_PASSWORD: "new-pw"},
            CONF_RTSP: {CONF_PASSWORD: "pw"},
        },
    )
    assert result["errors"] == {"base": "rtsp_login"}
    assert suggestions(result["data_schema"])[CONF_API] == {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: None,
    }
    assert browser_data(result["data_schema"])[CONF_API] == {CONF_USERNAME: "admin"}
    assert "new-pw" not in str(result)
    assert full_entry.data[CONF_API_PASSWORD] == "hunter2"


async def test_reconfigure_url_error_does_not_show_the_typed_api_password(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "http://admin:typed-pw@other.test:1984",
            CONF_API: {CONF_USERNAME: "admin", CONF_PASSWORD: "new-pw"},
            CONF_RTSP: {},
        },
    )
    assert result["errors"] == {CONF_URL: "url_login"}
    assert browser_data(result["data_schema"])[CONF_API] == {CONF_USERNAME: "admin"}
    assert "new-pw" not in str(result)
    assert "typed-pw" not in str(result)


@pytest.fixture
def username_only_entry(hass) -> MockConfigEntry:
    """An entry for go2rtc set up with `api.username` alone, monitoring off."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Gate",
        unique_id=entry_unique_id(URL, STREAM),
        data={
            CONF_URL: URL,
            CONF_STREAM: STREAM,
            CONF_MONITORING: False,
            CONF_API_USERNAME: "admin",
            CONF_API_PASSWORD: "",
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        ({CONF_USERNAME: "admin"}, {CONF_API_USERNAME: "admin", CONF_API_PASSWORD: ""}),
        # No saved password to keep, so the new username goes without one.
        ({CONF_USERNAME: "viewer"}, {CONF_API_USERNAME: "viewer", CONF_API_PASSWORD: ""}),
        (API_LOGIN, {CONF_API_USERNAME: "viewer", CONF_API_PASSWORD: "hunter2"}),
        ({}, {}),
    ],
    ids=["keep", "new-username", "add-password", "drop"],
)
async def test_reconfigure_username_only_api_login(
    hass, username_only_entry, aioclient_mock, api, expected
):
    result = await username_only_entry.start_reconfigure_flow(hass)
    shown = browser_data(result["data_schema"])
    assert shown[CONF_API] == {CONF_USERNAME: "admin"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: api, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert username_only_entry.data == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_MONITORING: False,
        **expected,
    }


async def test_reconfigure_adds_a_username_only_api_login(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {CONF_USERNAME: "admin"}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {
        CONF_URL: URL,
        CONF_STREAM: STREAM,
        CONF_API_USERNAME: "admin",
        CONF_API_PASSWORD: "",
    }
    # The reloaded entry checks with the username and an empty password.
    assert sent_headers(aioclient_mock)
    assert all(headers == ADMIN_HEADER for headers in sent_headers(aioclient_mock))


@pytest.mark.parametrize(
    ("url", "api", "rtsp", "errors"),
    [
        ("go2rtc", {CONF_USERNAME: "admin"}, {CONF_PASSWORD: "pw"}, {CONF_URL: "invalid_url"}),
        (URL, {CONF_USERNAME: "admin"}, {CONF_PASSWORD: "pw"}, {"base": "api_login"}),
        (URL, {CONF_USERNAME: "a:b", CONF_PASSWORD: "pw"}, {}, {"base": "api_login"}),
        (URL, {}, {CONF_PASSWORD: "pw"}, {"base": "rtsp_login"}),
    ],
    ids=["url-first", "api-second", "api-colon", "rtsp-last"],
)
async def test_reconfigure_reports_url_then_api_then_rtsp(hass, full_entry, url, api, rtsp, errors):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_API: api, CONF_RTSP: rtsp}
    )
    assert result["errors"] == errors


async def test_reconfigure_url_with_a_login_is_refused_and_shown_without_it(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "http://admin:typed-pw@other.test:1984", CONF_API: {}, CONF_RTSP: {}},
    )
    assert result["errors"] == {CONF_URL: "url_login"}
    assert browser_data(result["data_schema"])[CONF_URL] == "http://other.test:1984"
    assert "typed-pw" not in str(result)
    assert full_entry.data[CONF_URL] == URL


async def test_reconfigure_invalid_url_with_a_login_is_not_shown_back(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "ftp://admin:typed-pw@other.test", CONF_API: {}, CONF_RTSP: {}},
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    # The saved address is shown instead.
    assert browser_data(result["data_schema"])[CONF_URL] == URL
    assert "typed-pw" not in str(result)


async def test_reconfigure_in_the_browser_keeps_api_and_rtsp_settings(hass, full_entry):
    result = await full_entry.start_reconfigure_flow(hass)
    shown = browser_data(result["data_schema"])
    assert shown == {CONF_URL: URL, CONF_API: {CONF_USERNAME: "viewer"}, CONF_RTSP: RTSP_SHOWN}

    before = dict(full_entry.data)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], shown)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert full_entry.data == before


TYPED_API = {CONF_USERNAME: "viewer", CONF_PASSWORD: "new-pw"}


@pytest.mark.parametrize("fixture", ["entry", "full_entry"], ids=["none-saved", "saved"])
async def test_reconfigure_keeps_a_typed_api_password_through_another_error(
    hass, request, aioclient_mock, fixture
):
    """The form comes back without the typed password; sent back as is, it keeps it.

    Not the saved password, and not an empty one: the user typed a password and
    fixed another field, so the password must not be lost without a word.
    """
    go2rtc(aioclient_mock)
    config_entry = request.getfixturevalue(fixture)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: TYPED_API, CONF_RTSP: {CONF_PASSWORD: "pw"}}
    )
    assert result["errors"] == {"base": "rtsp_login"}
    assert "new-pw" not in str(result)
    resend = browser_data(result["data_schema"])
    assert resend[CONF_API] == {CONF_USERNAME: "viewer"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**resend, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_API_USERNAME] == "viewer"
    assert config_entry.data[CONF_API_PASSWORD] == "new-pw"


async def test_reconfigure_typed_api_password_goes_with_its_username(hass, entry, aioclient_mock):
    """A new username after the error does not take the password typed for another one."""
    go2rtc(aioclient_mock)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "go2rtc", CONF_API: TYPED_API, CONF_RTSP: {}}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {CONF_USERNAME: "admin"}, CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_USERNAME] == "admin"
    assert entry.data[CONF_API_PASSWORD] == ""


async def test_reconfigure_keeps_a_typed_rtsp_password_through_another_error(hass, login_entry):
    result = await login_entry.start_reconfigure_flow(hass)
    rtsp = {CONF_PORT: 18554, CONF_USERNAME: "admin", CONF_PASSWORD: "new-rtsp-pw"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {CONF_PASSWORD: "pw"}, CONF_RTSP: rtsp}
    )
    assert result["errors"] == {"base": "api_login"}
    assert "new-rtsp-pw" not in str(result)
    resend = browser_data(result["data_schema"])
    assert resend[CONF_RTSP] == {CONF_PORT: 18554, CONF_USERNAME: "admin"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**resend, CONF_API: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["reason"] == "reconfigure_successful"
    assert login_entry.data[CONF_RTSP_USERNAME] == "admin"
    assert login_entry.data[CONF_RTSP_PASSWORD] == "new-rtsp-pw"


@pytest.mark.parametrize("first_error", ["invalid_url", "cannot_connect", "no_streams"])
async def test_typed_api_password_survives_another_error_of_the_address_step(
    hass, aioclient_mock, first_error
):
    """Sent back as the browser fills it, the form logs in with the password typed before."""
    url = "go2rtc" if first_error == "invalid_url" else URL
    if first_error == "cannot_connect":
        aioclient_mock.get(STREAMS_URL, exc=aiohttp.ClientConnectionError())
    else:
        go2rtc(aioclient_mock, streams={})
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: url, CONF_API: API_LOGIN}
    )
    assert first_error in result["errors"].values()
    assert "hunter2" not in str(result)
    resend = browser_data(result["data_schema"])
    assert resend[CONF_API] == {CONF_USERNAME: "viewer"}

    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**resend, CONF_URL: URL}
    )
    assert result["step_id"] == "stream"
    assert sent_headers(aioclient_mock) == [VIEWER_HEADER]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: {}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["data"][CONF_API_PASSWORD] == "hunter2"


@pytest.mark.parametrize(
    ("typed_url", "shown_url"),
    [("go2rtc.lan:1984", "go2rtc.lan:1984"), ("ftp://go2rtc.lan@x:1984", DEFAULT_URL)],
)
async def test_typed_api_password_is_not_sent_to_the_default_address(
    hass, aioclient_mock, typed_url, shown_url
):
    """After invalid_url the form shows the typed address, or forgets the password.

    An address with `@` may hold a login, so it is not shown again; the default
    address then comes back and must not get the password typed for another host.
    """
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: typed_url, CONF_API: API_LOGIN}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    resend = browser_data(result["data_schema"])
    assert resend[CONF_URL] == shown_url

    aioclient_mock.get(f"{DEFAULT_URL}/api/streams", json=STREAMS)
    await hass.config_entries.flow.async_configure(result["flow_id"], resend)
    assert VIEWER_HEADER not in sent_headers(aioclient_mock)


async def test_refused_api_password_is_not_kept(hass, aioclient_mock):
    """After invalid_auth an empty password means none, as for go2rtc with `api.username` alone."""
    aioclient_mock.get(STREAMS_URL, status=401)
    result = await start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: URL, CONF_API: {CONF_USERNAME: "admin", CONF_PASSWORD: "x"}}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], browser_data(result["data_schema"])
    )
    assert result["step_id"] == "stream"
    assert sent_headers(aioclient_mock) == [ADMIN_HEADER]


async def test_typed_rtsp_password_survives_a_stream_error(hass, aioclient_mock, ffmpeg):
    result = await to_stream_step(hass, aioclient_mock)
    ffmpeg.outcome = Grab(8, b"", NOT_FOUND)
    go2rtc(aioclient_mock, streams={})
    rtsp = {CONF_USERNAME: "viewer", CONF_PASSWORD: "rtsp-pw"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert result["errors"] == {CONF_STREAM: "stream_not_found"}
    assert "rtsp-pw" not in str(result)
    resend = browser_data(result["data_schema"])
    assert resend[CONF_RTSP] == {CONF_USERNAME: "viewer"}

    ffmpeg.outcome = Grab(0, b"")
    go2rtc(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], resend)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_RTSP_PASSWORD] == "rtsp-pw"


async def test_rtsp_password_must_be_typed_again_after_a_failed_check(hass, aioclient_mock, ffmpeg):
    """The RTSP login may be why the check failed, so an empty password is not filled in."""
    result = await to_stream_step(hass, aioclient_mock)
    ffmpeg.outcome = Grab(1, b"", "method DESCRIBE failed: 401 Unauthorized")
    rtsp = {CONF_USERNAME: "viewer", CONF_PASSWORD: "wrong-pw"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STREAM: STREAM, CONF_NAME: "Driveway", CONF_RTSP: rtsp}
    )
    assert result["errors"] == {"base": "check_failed"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], browser_data(result["data_schema"])
    )
    assert result["errors"] == {"base": "rtsp_login"}


async def test_options_form_shows_current_values(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults[CONF_FAILURES] == 2
    assert defaults[CONF_SAME_FRAMES] == 3

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCAN_INTERVAL: 90.0,
            CONF_FRAME_TIMEOUT: 15.0,
            CONF_FAILURES: 4.0,
            CONF_SAME_FRAMES: 0,
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options == {
        CONF_SCAN_INTERVAL: 90,
        CONF_FRAME_TIMEOUT: 15,
        CONF_FAILURES: 4,
        CONF_SAME_FRAMES: 0,
    }
    assert all(type(value) is int for value in entry.options.values())


async def test_options_out_of_range_rejected(hass, entry, aioclient_mock):
    go2rtc(aioclient_mock)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL: 5, CONF_FRAME_TIMEOUT: 20, CONF_FAILURES: 2, CONF_SAME_FRAMES: 5},
        )
