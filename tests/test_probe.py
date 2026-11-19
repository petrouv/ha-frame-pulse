"""The probe module without Home Assistant.

Frame validation and classification, RTSP addresses and the port try, the ffmpeg run,
secret masking, the bounded body read and state transitions.
"""

import asyncio
import errno
import os
import socket
import sys
from datetime import UTC, datetime

import aiohttp
import pytest
from yarl import URL

from custom_components.frame_pulse import probe
from custom_components.frame_pulse.probe import (
    MAX_DETAIL_CHARS,
    MAX_FRAME_BYTES,
    MIN_FRAME_BYTES,
    READ_CHUNK_BYTES,
    ApiAccess,
    CameraState,
    FrameResult,
    Go2rtcAuthError,
    Go2rtcError,
    Grab,
    Limits,
    RtspAccess,
    Status,
    apply_result,
    classify_grab,
    disabled_state,
    ffmpeg_args,
    go2rtc_info,
    grab_frame,
    is_valid_jpeg,
    list_streams,
    mask_secrets,
    probe_frame,
    read_limited,
    rtsp_port_problem,
    rtsp_url,
    scrub,
    stream_label,
)

from .conftest import API_INFO, API_URL, NOT_FOUND, STREAM, STREAMS_URL, jpeg
from .conftest import URL as GO2RTC

NOW = datetime(2026, 11, 2, tzinfo=UTC)
LIMITS = Limits(failures=2, same_frames=3)


def step(state: CameraState, result: FrameResult, limits: Limits = LIMITS) -> CameraState:
    return apply_result(state, result, limits, NOW)


def good(digest: str = "a") -> FrameResult:
    return FrameResult(Status.OK, digest=digest)


@pytest.mark.parametrize(
    ("size", "valid"),
    [
        (MIN_FRAME_BYTES, True),
        (MAX_FRAME_BYTES, True),
        (MIN_FRAME_BYTES - 1, False),
        (MAX_FRAME_BYTES + 1, False),
    ],
    ids=["min", "max", "below_min", "above_max"],
)
def test_jpeg_size_limits(size, valid):
    assert is_valid_jpeg(jpeg(size=size)) is valid


@pytest.mark.parametrize(
    ("body", "valid"),
    [
        (jpeg() + b"\x00\x00", True),
        (jpeg() + b"\r\n", True),
        (jpeg() + b"\x00" * 30, True),  # the window counts the end marker too
        (jpeg() + b"\x00" * 31, False),
        (jpeg() + b"\xff", False),
        (b"\x00" + jpeg()[1:], False),
        (jpeg()[:-1] + b"\x00", False),
    ],
    ids=[
        "zero_padding",
        "crlf_padding",
        "padding_30",
        "padding_31",
        "ff_padding",
        "bad_start",
        "no_end",
    ],
)
def test_is_valid_jpeg(body, valid):
    assert is_valid_jpeg(body) is valid


@pytest.mark.parametrize(
    ("grab", "expected"),
    [
        (Grab(0, jpeg()), Status.OK),
        (Grab(0, b""), Status.NO_STREAM),
        (Grab(0, b"<html>"), Status.BAD_FRAME),
        (Grab(None), Status.FRAME_TIMEOUT),
        (Grab(8, b"", NOT_FOUND), Status.STREAM_NOT_FOUND),
        (Grab(1, b"", "Output file does not contain any stream"), Status.NO_STREAM),
        (Grab(195, b"", "Error opening input files: Connection refused"), Status.PROBE_ERROR),
        (Grab(1, b"", "method DESCRIBE failed: 401 Unauthorized"), Status.PROBE_ERROR),
        # musl, the C library of the ffmpeg in Home Assistant OS and the container.
        (Grab(195, b"", "Error opening input files: Host is unreachable"), Status.PROBE_ERROR),
        (Grab(195, b"", "Error opening input files: Network unreachable"), Status.PROBE_ERROR),
        (Grab(195, b"", "Error opening input files: Operation timed out"), Status.PROBE_ERROR),
        (Grab(195, b"", "Error opening input files: Name does not resolve"), Status.PROBE_ERROR),
        # ffmpeg 6.0 and older put the address into the last line: a number in a
        # stream name is not a status code.
        (Grab(1, b"", "rtsp://h:8554/cam404?video: Connection refused"), Status.PROBE_ERROR),
        (
            Grab(1, b"", "rtsp://h:8554/ch401?video: Invalid data found when processing input"),
            Status.NO_STREAM,
        ),
    ],
)
def test_classify_grab(grab, expected):
    assert classify_grab(grab) is expected


@pytest.mark.parametrize(
    ("base_url", "listen", "stream", "expected"),
    [
        ("http://go2rtc.test:1984", ":8554", "cam", "rtsp://go2rtc.test:8554/cam?video"),
        (
            "http://u:p@10.0.0.2:1984/go2rtc",
            "0.0.0.0:8555",
            "cam",
            "rtsp://10.0.0.2:8555/cam?video",
        ),
        ("http://[::1]:1984", "[::]:8554", "cam", "rtsp://[::1]:8554/cam?video"),
        ("http://h:1984", ":8554", "a b/c?d=1", "rtsp://h:8554/a%20b%2Fc%3Fd%3D1?video"),
        ("http://h:1984", "", "cam", None),
        ("http://h:1984", None, "cam", None),
    ],
)
def test_rtsp_url(base_url, listen, stream, expected):
    """The name is encoded whole: go2rtc decodes the path back into the stream name."""
    info = {"version": "1.9.9", "rtsp": {"listen": listen}} if listen is not None else {}
    assert rtsp_url(base_url, info, stream) == expected


@pytest.mark.parametrize(
    ("listen", "access", "expected"),
    [
        # Docker publishes the RTSP server on another port than go2rtc listens on.
        (":8554", RtspAccess(port=18554), "rtsp://h:18554/cam?video"),
        # A configured port is used even when go2rtc reports none: a proxy may serve it.
        ("", RtspAccess(port=554), "rtsp://h:554/cam?video"),
        (
            ":8554",
            RtspAccess(username="viewer", password="p@ss:w/rd?"),
            "rtsp://viewer:p%40ss%3Aw%2Frd%3F@h:8554/cam?video",
        ),
        (":8554", RtspAccess(username="a b", password="x"), "rtsp://a%20b:x@h:8554/cam?video"),
        (":8554", RtspAccess(password="orphan"), "rtsp://h:8554/cam?video"),
    ],
    ids=["port", "port-without-listen", "login", "login-space", "password-alone"],
)
def test_rtsp_url_with_access(listen, access, expected):
    info = {"version": "1.9.9", "rtsp": {"listen": listen}}
    assert rtsp_url("http://h:1984", info, "cam", access) == expected


def test_rtsp_access_repr_hides_the_password():
    assert "hunter2" not in repr(RtspAccess(username="viewer", password="hunter2"))


@pytest.mark.usefixtures("socket_enabled")
async def test_rtsp_port_problem_none_when_the_port_opens():
    accepted = asyncio.Event()

    async def accept(reader, writer):
        accepted.set()
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert await rtsp_port_problem(f"rtsp://127.0.0.1:{port}/cam?video", 5) is None
        await asyncio.wait_for(accepted.wait(), 5)


@pytest.mark.usefixtures("socket_enabled")
async def test_rtsp_port_problem_names_a_refusal():
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    assert await rtsp_port_problem(f"rtsp://127.0.0.1:{port}/cam?video", 5) == os.strerror(
        errno.ECONNREFUSED
    )


async def test_rtsp_port_problem_on_a_silent_port(monkeypatch):
    """A firewall that drops packets: the connection neither opens nor fails."""
    calls = []

    async def silent(host, port):
        calls.append((host, port))
        await asyncio.sleep(60)

    monkeypatch.setattr(asyncio, "open_connection", silent)
    assert await rtsp_port_problem("rtsp://[::1]:18554/cam?video", 0.1) == "no answer in 0.1 s"
    assert calls == [("::1", 18554)]


@pytest.mark.parametrize(
    ("error", "problem"),
    [
        (ConnectionResetError(), "ConnectionResetError"),
        # A resolver code is not an errno: 8 would read "Exec format error".
        (socket.gaierror(8, "nodename nor servname provided"), "nodename nor servname provided"),
        # Several addresses failed: asyncio lists them with their codes, the error has none.
        (
            OSError(
                "Multiple exceptions: [Errno 61] Connect call failed ('10.0.0.9', 8554), "
                "[Errno 65] Connect call failed ('fd00::9', 8554, 0, 0), "
                "[Errno 61] Connect call failed ('10.0.0.8', 8554)"
            ),
            f"{os.strerror(61)}; {os.strerror(65)}",
        ),
        (OSError("Multiple exceptions: a, b"), "OSError"),
        (OSError(errno.EHOSTUNREACH, "Connect call failed ('10.0.0.9', 8554)"), None),
    ],
    ids=["no-text", "resolver", "several", "several-without-codes", "errno"],
)
async def test_rtsp_port_problem_wording(monkeypatch, error, problem):
    async def fails(host, port):
        raise error

    monkeypatch.setattr(asyncio, "open_connection", fails)
    expected = problem or os.strerror(errno.EHOSTUNREACH)
    assert await rtsp_port_problem("rtsp://h:8554/cam?video", 5) == expected


FAKE_FFMPEG = """\
import os, sys, time
mode = os.environ["FAKE_FFMPEG"]
with open(os.environ["FAKE_FFMPEG_PID"], "w") as file:
    file.write(str(os.getpid()))
if args_file := os.environ.get("FAKE_FFMPEG_ARGS"):
    with open(args_file, "w") as file:
        file.write("\\n".join(sys.argv[1:]))
if mode == "frame":
    sys.stdout.buffer.write(bytes.fromhex(os.environ["FAKE_FFMPEG_FRAME"]))
elif mode == "error":
    sys.stderr.write("first line\\nError opening input files: Server returned 404 Not Found\\n")
    sys.exit(8)
else:
    time.sleep(60)
"""


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """A real process in place of ffmpeg; it records its PID to check that it is gone."""
    script = tmp_path / "ffmpeg"
    script.write_text(f"#!{sys.executable}\n{FAKE_FFMPEG}")
    script.chmod(0o755)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("FAKE_FFMPEG_PID", str(pid_file))
    monkeypatch.setenv("FAKE_FFMPEG_FRAME", jpeg().hex())

    def run(mode: str):
        monkeypatch.setenv("FAKE_FFMPEG", mode)
        return str(script)

    run.pid = lambda: int(pid_file.read_text())
    return run


def process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_grab_frame_returns_the_frame(fake_ffmpeg):
    assert await grab_frame(fake_ffmpeg("frame"), "rtsp://h/x", 10) == Grab(0, jpeg(), "")


def test_ffmpeg_args_leave_the_quality_to_ffmpeg_by_default():
    assert "-q:v" not in ffmpeg_args("rtsp://h/x")


def test_ffmpeg_args_with_a_quality():
    args = ffmpeg_args("rtsp://h/x", 5)
    at = args.index("mjpeg")
    assert args[at - 1 : at + 4] == ["-c:v", "mjpeg", "-q:v", "5", "-f"]


@pytest.mark.parametrize(("quality", "expected"), [(None, []), (3, ["-q:v", "3"])])
async def test_grab_frame_passes_the_quality_to_ffmpeg(
    fake_ffmpeg, tmp_path, monkeypatch, quality, expected
):
    args_file = tmp_path / "args"
    monkeypatch.setenv("FAKE_FFMPEG_ARGS", str(args_file))
    assert await grab_frame(fake_ffmpeg("frame"), "rtsp://h/x", 10, quality) == Grab(0, jpeg(), "")
    args = args_file.read_text().split("\n")
    assert args == ffmpeg_args("rtsp://h/x", quality)
    at = args.index("mjpeg") + 1
    assert args[at : at + len(expected)] == expected


async def test_grab_frame_keeps_the_last_error_line(fake_ffmpeg):
    assert await grab_frame(fake_ffmpeg("error"), "rtsp://h/x", 10) == Grab(8, b"", NOT_FOUND)


async def test_grab_frame_kills_ffmpeg_on_timeout(fake_ffmpeg):
    """A killed ffmpeg closes its RTSP session, so go2rtc keeps no consumer."""
    assert await grab_frame(fake_ffmpeg("hang"), "rtsp://h/x", 0.5) == Grab(None)
    assert process_gone(fake_ffmpeg.pid())


async def test_grab_frame_kills_ffmpeg_on_cancel(fake_ffmpeg, tmp_path):
    task = asyncio.create_task(grab_frame(fake_ffmpeg("hang"), "rtsp://h/x", 30))
    while not (tmp_path / "pid").exists():
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process_gone(fake_ffmpeg.pid())


async def test_grab_frame_without_ffmpeg_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await grab_frame(str(tmp_path / "missing"), "rtsp://h/x", 10)


def test_mask_secrets():
    text = mask_secrets("ffmpeg:rtsp://admin:pw@10.0.0.5/a?b=1 and RTSPS://u:p@host/x")
    assert "pw" not in text
    assert "u:p" not in text
    assert "rtsp://***" in text
    assert "rtsps://***" in text


def test_mask_secrets_keeps_the_text_around_an_address():
    text = mask_secrets("open http://plain.test:1984/api: 404; rtsp://u:p q@h/x: 401 Unauthorized")
    assert text == "open http://plain.test:1984/api: 404; rtsp://*** 401 Unauthorized"


def test_mask_secrets_any_scheme_with_credentials():
    text = mask_secrets("onvif://admin:pw@10.0.0.5 http://u:p@host:80/x http://plain.test:1984/api")
    assert "pw" not in text
    assert "u:p" not in text
    assert "onvif://***" in text
    assert "http://plain.test:1984/api" in text


@pytest.mark.parametrize(
    "text",
    [
        # A quote or a slash in the password must not end the mask early.
        "failed: url='rtsp://admin:pa'S3cr3t@10.0.0.5/stream'",
        "http://admin:pa'S3cr3t@10.0.0.5/x",
        "http://admin:pa/S3cr3t@10.0.0.5/x",
        # A secret in the query, not in `user:password@`.
        "http://10.0.0.5/snap.cgi?user=admin&pwd=S3cr3t",
        "tuya://host?device_id=1&client_secret=S3cr3t",
        # Whitespace in the password must not end the mask early either.
        "http://admin:pa ss S3cr3t@10.0.0.5/x: 401 Unauthorized",
        "rtsp://admin:pa S3cr3t@10.0.0.5/x",
    ],
)
def test_mask_secrets_hides_the_whole_address(text):
    assert "S3cr3t" not in mask_secrets(text)
    assert "10.0.0.5" not in mask_secrets(text)


@pytest.mark.parametrize(
    ("stream", "label"),
    [
        ("driveway_sd", "driveway_sd"),
        ("garage/sub", "garage/sub"),
        ("rtsp://admin:pa'S3cr3t@10.0.0.5/stream", "rtsp://***"),
        ("http://10.0.0.5/snap.cgi?user=admin&pwd=S3cr3t", "http://***"),
        ("rtmp://10.0.0.5/live2/S3cr3t", "rtmp://***"),
        ("ffmpeg:rtsp://a:S3cr3t@h/x#video=h264", "ffmpeg://***"),
        ("ring:?refresh_token=S3cr3t", "ring://***"),
        ("exec:ffmpeg -headers 'Authorization: S3cr3t' -i x", "exec://***"),
        ("cam S3cr3t@10.0.0.5", "***"),
    ],
)
def test_stream_label(stream, label):
    """A name that looks like a source is hidden entirely: a secret can be anywhere in it."""
    assert stream_label(stream) == label


@pytest.mark.parametrize(
    "stream",
    ["tuya://host?client_secret=S3cr3t&x=1", "rtsp://admin:pa'S3cr3t@10.0.0.5/s#video=h264"],
)
def test_scrub_hides_the_stream_also_url_encoded(stream):
    """A secret-bearing stream name is hidden as is and query-encoded.

    The encoded form appears in a go2rtc HTTP URL.
    """
    encoded = str(URL("http://go2rtc.test:1984/api/frame.jpeg").with_query(src=stream))
    text = scrub(f"400, message='Bad Request', url='{encoded}' and {stream}", stream)
    assert "S3cr3t" not in text
    assert "10.0.0.5" not in text


def test_scrub_cuts_long_text():
    assert len(scrub("Z" * 10_000, "cam")) <= MAX_DETAIL_CHARS


async def test_read_limited_stops_on_endless_body():
    """An endless body is read at most one chunk past the limit.

    Such a body is, for example, a video stream on a wrong port.
    """

    class Content:
        chunks = 0

        async def iter_chunked(self, size):
            while True:
                self.chunks += 1
                yield b"\x00" * size

    class Response:
        content = Content()

    response = Response()
    body = await read_limited(response, MAX_FRAME_BYTES)
    assert MAX_FRAME_BYTES < len(body) <= MAX_FRAME_BYTES + READ_CHUNK_BYTES
    assert response.content.chunks == MAX_FRAME_BYTES // READ_CHUNK_BYTES + 1


# "viewer:hunter2" and "оператор:пароль", Base64 of their UTF-8 bytes.
VIEWER_HEADER = {"Authorization": "Basic dmlld2VyOmh1bnRlcjI="}
CYRILLIC_HEADER = {"Authorization": "Basic 0L7Qv9C10YDQsNGC0L7RgDrQv9Cw0YDQvtC70Yw="}


@pytest.fixture
async def session(aioclient_mock):
    """A client session whose requests go to the HTTP mock."""
    client = aioclient_mock.create_session(asyncio.get_running_loop())
    yield client
    await client.close()


def sent_headers(aioclient_mock) -> list[dict]:
    return [call[3] or {} for call in aioclient_mock.mock_calls]


@pytest.mark.parametrize(
    ("api", "expected"),
    [
        (ApiAccess(username="viewer", password="hunter2"), VIEWER_HEADER),
        (ApiAccess(username="оператор", password="пароль"), CYRILLIC_HEADER),
    ],
    ids=["latin", "cyrillic"],
)
async def test_api_requests_carry_the_login(aioclient_mock, session, api, expected):
    aioclient_mock.get(API_URL, json=API_INFO)
    aioclient_mock.get(STREAMS_URL, json={STREAM: {}})
    assert await go2rtc_info(session, GO2RTC, api=api) == API_INFO
    assert await list_streams(session, GO2RTC, api=api) == [STREAM]
    assert sent_headers(aioclient_mock) == [expected, expected]


async def test_api_requests_without_a_login_carry_no_authorization(aioclient_mock, session):
    aioclient_mock.get(API_URL, json=API_INFO)
    aioclient_mock.get(STREAMS_URL, json={STREAM: {}})
    await go2rtc_info(session, GO2RTC)
    await list_streams(session, GO2RTC, api=ApiAccess())
    assert all("Authorization" not in headers for headers in sent_headers(aioclient_mock))


@pytest.mark.parametrize("call", [go2rtc_info, list_streams])
async def test_api_401_is_an_auth_error_without_the_login(aioclient_mock, session, call):
    aioclient_mock.get(API_URL, status=401)
    aioclient_mock.get(STREAMS_URL, status=401)
    with pytest.raises(Go2rtcAuthError) as caught:
        await call(session, GO2RTC, api=ApiAccess(username="viewer", password="hunter2"))
    assert str(caught.value) == "HTTP 401: the API login is missing or wrong"
    assert isinstance(caught.value, Go2rtcError)


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_other_api_codes_stay_plain(aioclient_mock, session, status):
    aioclient_mock.get(API_URL, status=status)
    with pytest.raises(Go2rtcError) as caught:
        await go2rtc_info(session, GO2RTC)
    assert type(caught.value) is Go2rtcError
    assert str(caught.value) == f"HTTP {status}"


def test_api_access_repr_hides_the_password():
    assert "hunter2" not in repr(ApiAccess(username="viewer", password="hunter2"))


def test_api_access_headers():
    assert ApiAccess().headers() == {}
    # go2rtc turns the login on by the username alone, so an empty password is still sent.
    assert ApiAccess(username="viewer").headers() == {"Authorization": "Basic dmlld2VyOg=="}


@pytest.fixture
def fake_grab(monkeypatch):
    """Replace the ffmpeg run in the probe module; the test sets `outcome`."""

    class Fake:
        outcome = Grab(0, jpeg())

        async def __call__(self, binary, url, timeout, quality=None):
            self.quality = quality
            return self.outcome

    fake = Fake()
    monkeypatch.setattr(probe, "grab_frame", fake)
    return fake


@pytest.mark.parametrize("quality", [None, 7])
async def test_probe_frame_passes_the_quality(aioclient_mock, session, fake_grab, quality):
    aioclient_mock.get(API_URL, json=API_INFO)
    await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, quality=quality)
    assert fake_grab.quality == quality


async def test_probe_frame_with_a_wrong_api_login_is_probe_error(aioclient_mock, session):
    aioclient_mock.get(API_URL, status=401)
    api = ApiAccess(username="viewer", password="hunter2")
    result = await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)
    assert result == FrameResult(
        Status.PROBE_ERROR,
        detail="go2rtc API failed: HTTP 401: the API login is missing or wrong",
        auth_failed=True,
    )
    assert sent_headers(aioclient_mock) == [VIEWER_HEADER]


async def test_probe_frame_other_api_failure_is_not_an_auth_failure(aioclient_mock, session):
    aioclient_mock.get(API_URL, status=403)
    result = await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5)
    assert result == FrameResult(Status.PROBE_ERROR, detail="go2rtc API failed: HTTP 403")


async def test_rtsp_login_refused_is_not_an_api_auth_failure(aioclient_mock, session, fake_grab):
    """ffmpeg's RTSP "401 Unauthorized" is the RTSP login, not the go2rtc API one."""
    aioclient_mock.get(API_URL, json=API_INFO)
    fake_grab.outcome = Grab(1, b"", "method DESCRIBE failed: 401 Unauthorized")
    result = await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5)
    assert result.status is Status.PROBE_ERROR
    assert not result.auth_failed


async def test_probe_frame_login_lost_after_a_failed_frame(aioclient_mock, session, monkeypatch):
    """The login stops working between the version before the frame and the one after."""
    aioclient_mock.get(API_URL, json=API_INFO)
    api = ApiAccess(username="viewer", password="hunter2")

    async def grab(binary, url, timeout, quality=None):
        aioclient_mock.clear_requests()
        aioclient_mock.get(API_URL, status=401)
        return Grab(0, b"")

    monkeypatch.setattr(probe, "grab_frame", grab)
    result = await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)
    assert result.status is Status.PROBE_ERROR
    assert result.auth_failed
    assert result.detail == (
        "go2rtc API failed: HTTP 401: the API login is missing or wrong (after 0 bytes)"
    )
    assert result.duration_ms is not None


async def test_probe_frame_sends_the_login_on_every_request(aioclient_mock, session, fake_grab):
    """The check and the re-checks after a failure: `/api`, then the stream list after 404."""
    aioclient_mock.get(API_URL, json=API_INFO)
    aioclient_mock.get(STREAMS_URL, json={STREAM: {}})
    api = ApiAccess(username="viewer", password="hunter2")

    assert (await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)).status is Status.OK
    fake_grab.outcome = Grab(0, b"")
    assert (await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)).status is (
        Status.NO_STREAM
    )
    fake_grab.outcome = Grab(8, b"", NOT_FOUND)
    assert (await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)).status is (
        Status.NO_STREAM
    )

    paths = [call[1].path for call in aioclient_mock.mock_calls]
    assert paths == ["/api", "/api", "/api", "/api", "/api/streams"]
    assert sent_headers(aioclient_mock) == [VIEWER_HEADER] * 5


async def test_probe_frame_login_lost_during_the_check(aioclient_mock, session, fake_grab):
    """The login stops working between the version and the stream list."""
    aioclient_mock.get(API_URL, json=API_INFO)
    aioclient_mock.get(STREAMS_URL, status=401)
    fake_grab.outcome = Grab(8, b"", NOT_FOUND)
    api = ApiAccess(username="viewer", password="hunter2")
    result = await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)
    assert result.status is Status.PROBE_ERROR
    assert result.auth_failed
    assert result.detail.startswith(
        "go2rtc API failed: HTTP 401: the API login is missing or wrong (after ffmpeg 8:"
    )
    assert "hunter2" not in result.detail
    assert "viewer" not in result.detail


async def test_api_login_is_not_in_the_frame_address(aioclient_mock, session, monkeypatch):
    """The API login goes in a header only, never on the ffmpeg command line."""
    aioclient_mock.get(API_URL, json=API_INFO)
    urls = []

    async def grab(binary, url, timeout, quality=None):
        urls.append(url)
        return Grab(0, jpeg())

    monkeypatch.setattr(probe, "grab_frame", grab)
    api = ApiAccess(username="viewer", password="hunter2")
    await probe_frame(session, GO2RTC, STREAM, "ffmpeg", 5, api=api)
    assert urls == [f"rtsp://go2rtc.test:8554/{STREAM}?video"]


async def test_api_request_without_answer_is_no_response(aioclient_mock, session):
    aioclient_mock.get(API_URL, exc=aiohttp.ClientConnectionError())
    with pytest.raises(Go2rtcError, match="^no response$"):
        await go2rtc_info(session, GO2RTC, api=ApiAccess(username="viewer", password="hunter2"))


def test_connectivity_threshold_and_return():
    state = step(CameraState(), good())
    assert state.connected is True

    state = step(state, FrameResult(Status.NO_STREAM))
    assert (state.status, state.connected, state.failures) == (Status.NO_STREAM, True, 1)

    state = step(state, FrameResult(Status.BAD_FRAME))
    assert (state.status, state.connected, state.failures) == (Status.BAD_FRAME, False, 2)

    state = step(state, good("b"))
    assert (state.status, state.connected, state.failures) == (Status.OK, True, 0)


def test_first_failure_after_start_is_unknown_not_off():
    state = step(CameraState(), FrameResult(Status.NO_STREAM))
    assert state.connected is None
    assert step(state, FrameResult(Status.NO_STREAM)).connected is False


@pytest.mark.parametrize("status", [Status.NO_STREAM, Status.BAD_FRAME, Status.FRAME_TIMEOUT])
def test_camera_failures_count_towards_the_threshold(status):
    state = step(step(CameraState(), good()), FrameResult(status))
    assert (state.status, state.connected, state.failures) == (status, True, 1)
    assert step(state, FrameResult(status)).connected is False


@pytest.mark.parametrize("status", [Status.PROBE_ERROR, Status.STREAM_NOT_FOUND])
def test_setup_errors_keep_counter_and_connectivity(status):
    start = CameraState(status=Status.NO_STREAM, connected=True, failures=1)
    state = step(start, FrameResult(status))
    assert (state.status, state.connected, state.failures) == (status, True, 1)


def test_frozen_after_n_identical_frames():
    state = CameraState()
    for _ in range(2):
        state = step(state, good())
        assert state.status is Status.OK
    state = step(state, good())
    assert state.status is Status.FROZEN
    assert state.connected is True


def test_frozen_keeps_last_good_frame_time():
    earlier = datetime(2026, 11, 1, tzinfo=UTC)
    start = CameraState(same_frames=2, last_digest="a", last_frame_at=earlier)
    assert step(start, good()).last_frame_at == earlier


def test_repeated_frame_does_not_move_last_frame_time():
    earlier = datetime(2026, 11, 1, tzinfo=UTC)
    start = CameraState(same_frames=1, last_digest="a", last_frame_at=earlier)
    state = step(start, good())
    assert state.status is Status.OK
    assert state.last_frame_at == earlier
    assert step(state, good("b")).last_frame_at == NOW


def test_failed_check_keeps_frozen():
    state = CameraState()
    for _ in range(3):
        state = step(state, good())
    assert state.frozen is True

    state = step(state, FrameResult(Status.NO_STREAM))
    assert (state.status, state.frozen) == (Status.NO_STREAM, True)
    assert step(state, good("b")).frozen is False


def test_different_frame_ends_frozen():
    state = CameraState(status=Status.FROZEN, frozen=True, same_frames=9, last_digest="a")
    state = step(state, good("b"))
    assert (state.status, state.frozen, state.same_frames) == (Status.OK, False, 1)


def test_failed_check_does_not_break_series():
    state = CameraState(same_frames=2, last_digest="a")
    state = step(state, FrameResult(Status.PROBE_ERROR))
    assert step(state, good()).status is Status.FROZEN


def test_frozen_check_off_with_zero():
    limits = Limits(failures=2, same_frames=0)
    state = CameraState()
    for _ in range(10):
        state = step(state, good(), limits)
    assert state.status is Status.OK


def test_disabled_state_resets_counters():
    state = disabled_state(
        CameraState(connected=False, failures=5, frozen=True, last_digest="a", last_frame_at=NOW)
    )
    assert state == CameraState(status=Status.DISABLED, last_frame_at=NOW)


@pytest.mark.parametrize("status", [Status.FROZEN, Status.DISABLED])
def test_derived_status_is_not_a_check_result(status):
    with pytest.raises(ValueError):
        step(CameraState(), FrameResult(status))
