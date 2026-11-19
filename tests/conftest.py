"""Shared fixtures."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_URL
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.frame_pulse.const import (
    CONF_FAILURES,
    CONF_SAME_FRAMES,
    CONF_STREAM,
    DOMAIN,
)
from custom_components.frame_pulse.coordinator import entry_unique_id
from custom_components.frame_pulse.probe import Grab

URL = "http://go2rtc.test:1984"
STREAM = "driveway_sd"
API_URL = f"{URL}/api"
STREAMS_URL = f"{URL}/api/streams"
API_INFO = {"version": "1.9.9", "rtsp": {"listen": ":8554"}}
RTSP_URL = f"rtsp://go2rtc.test:8554/{STREAM}?video"
NOT_FOUND = "Error opening input files: Server returned 404 Not Found"
SECRET_URL = "rtsp://admin:hunter2@10.0.0.5/stream"


def jpeg(seed: int = 0, size: int = 2048) -> bytes:
    """Build a body that passes the frame check."""
    return b"\xff\xd8\xff" + seed.to_bytes(4) + b"\x00" * (size - 9) + b"\xff\xd9"


class Ffmpeg:
    """Stands in for the ffmpeg run: returns the set outcome, records RTSP addresses and timeouts.

    Also stands in for the TCP try of the RTSP port that follows a frame timeout.

    Attributes:
        outcome: What the next run returns, or the exception it raises.
        hook: Awaited inside every run, to hold a run or count parallel ones.
        urls: RTSP addresses of the runs, in order.
        timeouts: Frame timeouts of the runs, in order.
        qualities: JPEG qualities of the runs, in order: `None` for ffmpeg's own choice.
        port_problem: What the port try returns: `None` when the port opens.
        port_tries: RTSP addresses whose port was tried.
    """

    def __init__(self) -> None:
        self.outcome: Grab | OSError = Grab(0, jpeg())
        self.hook: Callable[[], Awaitable[None]] | None = None
        self.urls: list[str] = []
        self.timeouts: list[float] = []
        self.qualities: list[int | None] = []
        self.port_problem: str | None = None
        self.port_tries: list[str] = []

    async def __call__(
        self, binary: str, url: str, timeout: float, quality: int | None = None
    ) -> Grab:
        self.urls.append(url)
        self.timeouts.append(timeout)
        self.qualities.append(quality)
        if self.hook is not None:
            await self.hook()
        if isinstance(self.outcome, OSError):
            raise self.outcome
        return self.outcome

    async def try_port(self, url: str, timeout: float) -> str | None:
        self.port_tries.append(url)
        return self.port_problem


@pytest.fixture
async def ffmpeg(hass) -> Ffmpeg:
    """Set up the Home Assistant ffmpeg integration.

    The ffmpeg run and the RTSP port try are replaced by fakes.
    """
    assert await async_setup_component(hass, "ffmpeg", {})
    fake = Ffmpeg()
    with (
        patch("custom_components.frame_pulse.probe.grab_frame", fake),
        patch("custom_components.frame_pulse.probe.rtsp_port_problem", fake.try_port),
    ):
        yield fake


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading the integration from custom_components."""
    return


@pytest.fixture(autouse=True)
def no_unexpected_errors(caplog):
    """Fail a test whose integration logged an error.

    The coordinator turns any exception of a check, such as a request with no mock
    behind it, into an "Unexpected error fetching" line, so the test would pass.
    """
    yield
    errors = [
        record.getMessage()
        for record in caplog.get_records("call")
        if record.levelno >= logging.ERROR and record.name.startswith("custom_components.")
    ]
    assert not errors


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    """Config entry added to hass but not set up.

    Disconnected after 2 failures in a row, frozen after 3 identical frames in a row.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Driveway",
        unique_id=entry_unique_id(URL, STREAM),
        data={CONF_URL: URL, CONF_STREAM: STREAM},
        options={CONF_FAILURES: 2, CONF_SAME_FRAMES: 3},
        minor_version=2,
    )
    config_entry.add_to_hass(hass)
    return config_entry
