"""Frame check through go2rtc and camera state calculation.

The module does not depend on Home Assistant: aiohttp, an ffmpeg process and
pure functions. Pipeline: frame from go2rtc RTSP -> classification -> state transition.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import time
from contextlib import AbstractAsyncContextManager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from urllib.parse import quote

import aiohttp
from yarl import URL

JPEG_START = b"\xff\xd8\xff"
JPEG_END = b"\xff\xd9"
# A uniform frame of a low-resolution stream takes only a few hundred bytes.
MIN_FRAME_BYTES = 128
# Padding after the end marker is tolerated. The window counts the marker itself,
# so up to 30 padding bytes pass. ffmpeg writes none; the window is a safety margin.
JPEG_TAIL_BYTES = 32
JPEG_PADDING = b"\x00\r\n"
MAX_FRAME_BYTES = 10 * 1024 * 1024
API_TIMEOUT = 5
# Caps on go2rtc API replies, so a wrong service on the port cannot fill memory.
# `/api` is a few hundred bytes. `/api/streams` lists every stream with its sources
# and clients, so it gets a far larger cap; a bigger reply is not go2rtc.
MAX_INFO_BYTES = 64 * 1024
MAX_STREAMS_BYTES = 16 * 1024 * 1024

READ_CHUNK_BYTES = 64 * 1024
# A log line carries a reason, not a server reply: aiohttp can put kilobytes of it into an error.
MAX_DETAIL_CHARS = 200

SECRET = "***"
# A go2rtc source starts with a scheme: `rtsp://`, `ffmpeg:`, `exec:`, `ring:?token=` and so on.
_SOURCE_SCHEME = re.compile(r"\s*([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
# Characters that a plain stream name does not need but a secret-bearing source does.
_SOURCE_MARKS = re.compile(r"[@?=&\s]|://")
# A URL in free text runs up to whitespace: a quote or a slash can be part of a password.
# A password can hold whitespace too, so when an `@` follows later on the same line,
# before another URL, the URL runs up to it as well.
_URL_IN_TEXT = re.compile(
    r"\b([a-z][a-z0-9+.\-]*)://"
    r"(?:(?:(?!\b[a-z][a-z0-9+.\-]*://)[^@\n])*@)?\S*",
    re.IGNORECASE,
)
# asyncio lists every failed address in one error: "[Errno 61] Connect call failed (address)".
_ERRNO_IN_TEXT = re.compile(r"\[Errno (\d+)\]")
# ffmpeg errors that mean the check cannot use the go2rtc RTSP server: it is
# unreachable or refuses the RTSP login.
# The texts come from the C library, so both glibc/macOS and musl (the ffmpeg in
# Home Assistant OS and the container) wordings are listed. Status codes are
# matched by their phrase: a bare number can be part of a stream name.
_BROKEN_CHECK = re.compile(
    r"Unauthorized|Connection refused|No route to host"
    r"|Host is unreachable|Network is unreachable|Network unreachable"
    r"|Connection timed out|Operation timed out"
    r"|Name or service not known|nodename nor servname|Name does not resolve",
    re.IGNORECASE,
)
_NOT_FOUND = re.compile(r"404 Not Found", re.IGNORECASE)


class Status(StrEnum):
    """Camera status: a check result or a derived state (`frozen`, `disabled`)."""

    OK = "ok"
    FROZEN = "frozen"
    NO_STREAM = "no_stream"
    BAD_FRAME = "bad_frame"
    FRAME_TIMEOUT = "frame_timeout"
    STREAM_NOT_FOUND = "stream_not_found"
    PROBE_ERROR = "probe_error"
    DISABLED = "disabled"


class Go2rtcError(Exception):
    """go2rtc is unreachable or replied in an unexpected way."""


class Go2rtcAuthError(Go2rtcError):
    """The go2rtc API asks for a login and the one sent is missing or wrong (HTTP 401)."""


@dataclass(frozen=True)
class FrameResult:
    """Result of a single frame request.

    Attributes:
        status: Status before the freeze check: `ok` means a valid frame.
        digest: SHA-256 of a valid frame.
        detail: Reason for the log, already free of addresses and of a secret-bearing stream name.
        duration_ms: How long ffmpeg ran, `None` if it did not start.
    """

    status: Status
    digest: str | None = None
    detail: str = ""
    duration_ms: int | None = None


@dataclass(frozen=True)
class Limits:
    """Thresholds from the camera options.

    Attributes:
        failures: Failures in a row before "disconnected".
        same_frames: Identical frames in a row before "frozen", 0 turns the check off.
    """

    failures: int
    same_frames: int


@dataclass(frozen=True)
class CameraState:
    """In-memory camera state.

    Attributes:
        status: Last status, `None` before the first check.
        connected: Connection; `None` until the first valid frame or until failures
            in a row reach the threshold.
        failures: Failures in a row.
        same_frames: How many times in a row the same frame was received.
        frozen: The picture is frozen; a failed check does not clear it.
        last_digest: Digest of the last frame.
        last_frame_at: Time when the picture last changed (UTC).
        last_check_at: Time of the last check (UTC).
        duration_ms: How long ffmpeg ran in the last check, `None` if it did not run.
    """

    status: Status | None = None
    connected: bool | None = None
    failures: int = 0
    same_frames: int = 0
    frozen: bool = False
    last_digest: str | None = None
    last_frame_at: datetime | None = None
    last_check_at: datetime | None = None
    duration_ms: int | None = None


def stream_label(stream: str) -> str:
    """Return a stream name that is safe to show in the UI, the log and diagnostics.

    go2rtc names a stream after its source when the stream is requested by
    address, and a source can carry a secret anywhere: in `user:password@`, in
    the query, in the path or in `exec:` arguments. So a name that starts with a
    scheme is hidden and only the scheme is kept, and any other name with `@`,
    `?`, `=`, `&`, whitespace or `://` is hidden entirely.
    """
    if match := _SOURCE_SCHEME.match(stream):
        return f"{match[1].lower()}://{SECRET}"
    if _SOURCE_MARKS.search(stream):
        return SECRET
    return stream


def _mask_url(match: re.Match[str]) -> str:
    scheme, url = match[1].lower(), match[0]
    if scheme in ("rtsp", "rtsps") or any(mark in url for mark in "@?="):
        return f"{scheme}://{SECRET}"
    return url


def mask_secrets(text: str) -> str:
    """Hide camera addresses in free text: they can carry a login, a password or a token.

    Every `rtsp://` and `rtsps://` address is hidden, and so is any other URL with
    `@` (a login) or `?`/`=` (a query). A URL that has none of them, such as the
    go2rtc address, stays readable. A login with whitespace in the password is
    hidden whole: the URL runs up to the `@` that follows later on the line, so the
    text between them is hidden too.
    """
    return _URL_IN_TEXT.sub(_mask_url, text)


def scrub(text: str, stream: str) -> str:
    """Make an error text safe for the log: no secret-bearing name or address, bounded length.

    A stream name that may carry a secret (see `stream_label`) is replaced by its label,
    both as is and query-encoded; a plain name stays. The RTSP address, which carries
    the name percent-encoded in its path, is hidden whole by the address mask.
    """
    label = stream_label(stream)
    if label != stream:
        encoded = str(URL("http://x/").with_query(src=stream)).partition("src=")[2]
        for form in sorted({stream, encoded}, key=len, reverse=True):
            text = text.replace(form, label)
    return cut(mask_secrets(text))


def cut(text: str) -> str:
    """Bound the length of a log reason."""
    if len(text) > MAX_DETAIL_CHARS:
        text = text[: MAX_DETAIL_CHARS - 1] + "…"
    return text


def is_valid_jpeg(body: bytes) -> bool:
    """Check that a body looks like a complete JPEG of an allowed size."""
    return (
        MIN_FRAME_BYTES <= len(body) <= MAX_FRAME_BYTES
        and body.startswith(JPEG_START)
        and body[-JPEG_TAIL_BYTES:].rstrip(JPEG_PADDING).endswith(JPEG_END)
    )


async def read_limited(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read a body, giving up right after `limit` bytes.

    The timeout limits only the time. Without this cap an endless response, such
    as a video stream on a wrong port, would be buffered in memory until it fires.
    """
    body = bytearray()
    async for chunk in response.content.iter_chunked(READ_CHUNK_BYTES):
        body += chunk
        if len(body) > limit:
            break
    return bytes(body)


@dataclass(frozen=True)
class ApiAccess:
    """The go2rtc API login (`api.username` and `api.password`).

    go2rtc checks it with HTTP Basic authentication, except for requests from
    a loopback address (127.x or ::1) unless `api.local_auth` is on. A request
    to the LAN address of the go2rtc machine itself is checked.

    Attributes:
        username: API login, empty when go2rtc asks for none.
        password: API password, left out of `repr`; may be empty, since go2rtc
            turns its login on with `api.username` alone.
    """

    username: str = ""
    password: str = field(default="", repr=False)

    def headers(self) -> dict[str, str]:
        """Return the request headers that carry the login, none without a username.

        go2rtc compares the bytes as they are, so the login is encoded as UTF-8:
        aiohttp's own helper uses Latin-1, which cannot encode a Cyrillic password.
        """
        if not self.username:
            return {}
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}


NO_API = ApiAccess()


async def _get_json(
    session: aiohttp.ClientSession, url: str, limit: int, api: ApiAccess = NO_API
) -> object:
    """Return the JSON body of a go2rtc API reply.

    The login goes in a header, never in the address, so it reaches no error
    text; aiohttp drops the header on a redirect to another host.

    Raises:
        Go2rtcAuthError: go2rtc refused the login (HTTP 401).
        Go2rtcError: No reply, another HTTP code, an oversized or broken body.
            The text never carries the body: `/api/streams` holds passwords.
    """
    try:
        async with session.get(
            url, headers=api.headers(), timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
        ) as response:
            if response.status == 401:
                raise Go2rtcAuthError("HTTP 401: the API login is missing or wrong")
            if response.status != 200:
                raise Go2rtcError(f"HTTP {response.status}")
            body = await read_limited(response, limit)
    except TimeoutError, aiohttp.ClientError:
        raise Go2rtcError("no response") from None
    if len(body) > limit:
        raise Go2rtcError("reply too large")
    try:
        return json.loads(body)
    except ValueError:
        raise Go2rtcError("unexpected response format") from None


async def go2rtc_info(
    session: aiohttp.ClientSession, base_url: str, *, api: ApiAccess = NO_API
) -> dict:
    """Return the `GET /api` reply.

    Raises:
        Go2rtcAuthError: go2rtc refused the API login.
        Go2rtcError: No proper go2rtc reply: no response, another HTTP code, or a
            body that is oversized, broken or has no `version`.
    """
    payload = await _get_json(session, f"{base_url}/api", MAX_INFO_BYTES, api)
    if not isinstance(payload, dict) or "version" not in payload:
        raise Go2rtcError("not a go2rtc reply")
    return payload


async def list_streams(
    session: aiohttp.ClientSession, base_url: str, *, api: ApiAccess = NO_API
) -> list[str]:
    """Return the go2rtc stream names.

    The `/api/streams` response carries camera logins and passwords, so only its
    keys are used and the response itself is never stored or logged.

    Raises:
        Go2rtcAuthError: go2rtc refused the API login.
        Go2rtcError: go2rtc is unreachable or the response cannot be parsed.
    """
    payload = await _get_json(session, f"{base_url}/api/streams", MAX_STREAMS_BYTES, api)
    if not isinstance(payload, dict):
        raise Go2rtcError("unexpected response format")
    return sorted(payload)


@dataclass(frozen=True)
class RtspAccess:
    """How the check reaches the go2rtc RTSP server.

    Attributes:
        port: Port to connect to, `None` for the one go2rtc listens on. Docker
            can publish the RTSP server on another port.
        username: RTSP login, empty when go2rtc asks for none.
        password: RTSP password, left out of `repr`.
    """

    port: int | None = None
    username: str = ""
    password: str = field(default="", repr=False)


NO_ACCESS = RtspAccess()

# ffmpeg keeps the login of an RTSP address (`user:password`, percent-encoded)
# in a 128-byte buffer and silently cuts a longer one, so the server gets a
# wrong password.
MAX_RTSP_LOGIN = 127


def rtsp_login(access: RtspAccess) -> str:
    """Return the login part of the RTSP address, `user:password` percent-encoded."""
    if not access.username:
        return ""
    return f"{quote(access.username, safe='')}:{quote(access.password, safe='')}"


def rtsp_url(base_url: str, info: dict, stream: str, access: RtspAccess = NO_ACCESS) -> str | None:
    """Return the go2rtc RTSP address of a stream, `None` if the RTSP server is off.

    The host is the one of the go2rtc address. The port is the configured one,
    or else the `rtsp.listen` setting that `GET /api` reports; a configured port
    is used even when go2rtc reports none, since a proxy may serve it. Only the
    video track is requested.
    """
    if access.port is not None:
        port = str(access.port)
    else:
        rtsp = info.get("rtsp")
        listen = rtsp.get("listen") if isinstance(rtsp, dict) else None
        port = listen.rpartition(":")[2] if isinstance(listen, str) else ""
        if not port.isdigit():
            return None
    host = URL(base_url).raw_host or ""
    if ":" in host:
        host = f"[{host}]"
    login = rtsp_login(access)
    if login:
        login += "@"
    return f"rtsp://{login}{host}:{port}/{quote(stream, safe='')}?video"


async def rtsp_port_problem(url: str, timeout: float) -> str | None:
    """Return why a TCP connection to the RTSP server fails, `None` if it opens.

    Asked after a frame timeout: a port that drops connections silently, such as
    one closed by a firewall, keeps ffmpeg waiting just like a hanging camera.
    The connection is closed at once, before any RTSP request.
    """
    parsed = URL(url)
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(parsed.raw_host, parsed.port)
    except TimeoutError:
        return f"no answer in {timeout} s"
    except OSError as err:
        # asyncio words a refusal as "Connect call failed (address)": the code says more.
        # A resolver error code is not an errno, so its own text is kept.
        if err.errno and not isinstance(err, socket.gaierror):
            return os.strerror(err.errno)
        if err.strerror:
            return err.strerror
        # A host with several addresses fails with one error listing them all, with
        # no code of its own: only the codes are kept, the addresses stay out of the log.
        if codes := dict.fromkeys(_ERRNO_IN_TEXT.findall(str(err))):
            return "; ".join(os.strerror(int(code)) for code in codes)
        return type(err).__name__
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    return None


@dataclass(frozen=True)
class Grab:
    """Outcome of one ffmpeg run.

    Attributes:
        returncode: Exit code, `None` if ffmpeg was killed after the timeout.
        frame: Standard output: the JPEG of the first keyframe.
        error: Last line of the error output.
    """

    returncode: int | None
    frame: bytes = b""
    error: str = ""


def ffmpeg_args(url: str) -> list[str]:
    """Return ffmpeg arguments that save the first keyframe of a stream as JPEG.

    Without the probe limits ffmpeg spends about 3 s analyzing the stream; with
    them a frame takes about as long as go2rtc's own snapshot.
    """
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-rtsp_transport",
        "tcp",
        "-analyzeduration",
        "0",
        "-probesize",
        "32",
        "-skip_frame",
        "nokey",
        "-i",
        url,
        "-frames:v",
        "1",
        "-an",
        "-c:v",
        "mjpeg",
        "-f",
        "image2pipe",
        "-",
    ]


async def grab_frame(binary: str, url: str, timeout: float) -> Grab:
    """Run ffmpeg once and kill it when the timeout runs out or the check is cancelled.

    Killing the process closes its RTSP session, so go2rtc drops the consumer
    at once and nothing is left behind by a hanging camera.

    Raises:
        OSError: ffmpeg cannot be started.
    """
    process = await asyncio.create_subprocess_exec(
        binary,
        *ffmpeg_args(url),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            frame, error = await process.communicate()
    except TimeoutError:
        return Grab(None)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    lines = error.decode(errors="replace").strip().splitlines()
    return Grab(process.returncode, frame, lines[-1] if lines else "")


def classify_grab(grab: Grab) -> Status:
    """Derive a status from an ffmpeg run.

    go2rtc answers RTSP 404 both for an unknown stream and for a camera it cannot
    reach, so `stream_not_found` here is only a candidate. Network errors and a
    missing or wrong RTSP login mean the check cannot use go2rtc's RTSP server,
    not that the camera is down.
    """
    if grab.returncode is None:
        return Status.FRAME_TIMEOUT
    if grab.returncode == 0:
        if not grab.frame:
            return Status.NO_STREAM
        return Status.OK if is_valid_jpeg(grab.frame) else Status.BAD_FRAME
    if _NOT_FOUND.search(grab.error):
        return Status.STREAM_NOT_FOUND
    if _BROKEN_CHECK.search(grab.error):
        return Status.PROBE_ERROR
    return Status.NO_STREAM


async def _resolve_not_found(
    session: aiohttp.ClientSession, base_url: str, stream: str, detail: str, api: ApiAccess
) -> tuple[Status, str]:
    """Tell an unknown stream from a camera go2rtc cannot reach, both RTSP 404.

    The stream list is asked instead of the version: it answers both questions.
    """
    try:
        streams = await list_streams(session, base_url, api=api)
    except Go2rtcError as err:
        return Status.PROBE_ERROR, _api_failed_after(err, detail)
    if stream in streams:
        return Status.NO_STREAM, "go2rtc cannot reach the camera (RTSP 404)"
    return Status.STREAM_NOT_FOUND, "go2rtc has no such stream (RTSP 404)"


def _api_failed_after(err: Go2rtcError, detail: str) -> str:
    """Keep the reason of the failed frame next to the API failure."""
    return cut(f"go2rtc API failed: {err} (after {detail})")


async def probe_frame(
    session: aiohttp.ClientSession,
    base_url: str,
    stream: str,
    binary: str,
    timeout: float,
    limit: AbstractAsyncContextManager | None = None,
    *,
    access: RtspAccess = NO_ACCESS,
    api: ApiAccess = NO_API,
) -> FrameResult:
    """Fetch one frame through go2rtc and tell a dead camera from a broken check.

    Only the ffmpeg run is held under `limit` and timed: the API requests start
    no ffmpeg. After a camera failure go2rtc is asked again (its version, or the
    stream list after RTSP 404), so a go2rtc that died or hung during the check
    shows up as `probe_error`, not as a dead camera. After a frame timeout the
    RTSP port is also tried, so a port that drops connections silently is
    `probe_error` too. Every API request carries the API login, `access` is
    the RTSP one.
    """
    try:
        info = await go2rtc_info(session, base_url, api=api)
    except Go2rtcError as err:
        return FrameResult(Status.PROBE_ERROR, detail=f"go2rtc API failed: {err}")
    url = rtsp_url(base_url, info, stream, access)
    if url is None:
        return FrameResult(Status.PROBE_ERROR, detail="the go2rtc RTSP server is off")

    async with limit or nullcontext():
        started = time.monotonic()
        try:
            grab = await grab_frame(binary, url, timeout)
        except OSError as err:
            detail = f"ffmpeg cannot start: {err.strerror or type(err).__name__}"
            return FrameResult(Status.PROBE_ERROR, detail=detail)
        duration_ms = round((time.monotonic() - started) * 1000)

    status = classify_grab(grab)
    if status is Status.OK:
        digest = hashlib.sha256(grab.frame).hexdigest()
        return FrameResult(status, digest=digest, duration_ms=duration_ms)
    if status is Status.FRAME_TIMEOUT:
        detail = f"no frame in {timeout} s"
    elif grab.returncode == 0:
        detail = f"{len(grab.frame)} bytes"
    else:
        detail = scrub(f"ffmpeg {grab.returncode}: {grab.error}", stream)

    if status is Status.STREAM_NOT_FOUND:
        status, detail = await _resolve_not_found(session, base_url, stream, detail, api)
    elif status is not Status.PROBE_ERROR:
        try:
            await go2rtc_info(session, base_url, api=api)
        except Go2rtcError as err:
            status, detail = Status.PROBE_ERROR, _api_failed_after(err, detail)
        else:
            if status is Status.FRAME_TIMEOUT and (
                problem := await rtsp_port_problem(url, API_TIMEOUT)
            ):
                detail = cut(f"the go2rtc RTSP port does not answer: {problem} (after {detail})")
                status = Status.PROBE_ERROR
    return FrameResult(status, detail=detail, duration_ms=duration_ms)


def apply_result(
    state: CameraState,
    result: FrameResult,
    limits: Limits,
    now: datetime,
) -> CameraState:
    """Calculate the new camera state from a check result.

    Identical frames are counted among received ones: a failed check does not
    break the series and does not clear `frozen`. A repeated frame does not move
    `last_frame_at`. `stream_not_found` and `probe_error` say nothing about the
    camera, so they touch neither the failure counter nor the connection.
    """
    state = replace(state, last_check_at=now, duration_ms=result.duration_ms)

    if result.status is Status.OK:
        new_frame = result.digest != state.last_digest
        same_frames = 1 if new_frame else state.same_frames + 1
        frozen = limits.same_frames > 0 and same_frames >= limits.same_frames
        return replace(
            state,
            status=Status.FROZEN if frozen else Status.OK,
            connected=True,
            failures=0,
            same_frames=same_frames,
            frozen=frozen,
            last_digest=result.digest,
            last_frame_at=now if new_frame else state.last_frame_at,
        )

    if result.status in (Status.NO_STREAM, Status.BAD_FRAME, Status.FRAME_TIMEOUT):
        failures = state.failures + 1
        return replace(
            state,
            status=result.status,
            failures=failures,
            connected=False if failures >= limits.failures else state.connected,
        )

    if result.status in (Status.STREAM_NOT_FOUND, Status.PROBE_ERROR):
        return replace(state, status=result.status)

    # `frozen` and `disabled` are derived states, never a check result.
    raise ValueError(f"not a check result: {result.status}")


def disabled_state(state: CameraState) -> CameraState:
    """Return the state of a camera in planned shutdown.

    Counters are reset, so counting restarts when monitoring is turned on. Only
    the time when the picture last changed is kept.
    """
    return CameraState(status=Status.DISABLED, last_frame_at=state.last_frame_at)
