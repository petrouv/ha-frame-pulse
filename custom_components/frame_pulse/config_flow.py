"""UI setup: go2rtc address and API login, stream, name and RTSP access.

Also reauthentication, reconfigure and options.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import voluptuous as vol
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .const import (
    CONF_API,
    CONF_API_PASSWORD,
    CONF_API_USERNAME,
    CONF_FAILURES,
    CONF_FRAME_TIMEOUT,
    CONF_RTSP,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_SAME_FRAMES,
    CONF_STREAM,
    DEFAULT_FAILURES,
    DEFAULT_FRAME_TIMEOUT,
    DEFAULT_SAME_FRAMES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_URL,
    DOMAIN,
    MAX_FAILURES,
    MAX_FRAME_TIMEOUT,
    MAX_SAME_FRAMES,
    MAX_SCAN_INTERVAL,
    MIN_FAILURES,
    MIN_FRAME_TIMEOUT,
    MIN_SAME_FRAMES,
    MIN_SCAN_INTERVAL,
)
from .coordinator import (
    FramePulseConfigEntry,
    api_access,
    entry_unique_id,
    probe_semaphore,
    rtsp_access,
)
from .probe import (
    MAX_RTSP_LOGIN,
    FrameResult,
    Go2rtcAuthError,
    Go2rtcError,
    Status,
    list_streams,
    probe_frame,
    rtsp_login,
    stream_label,
)

_RTSP_KEYS = (CONF_RTSP_PORT, CONF_RTSP_USERNAME, CONF_RTSP_PASSWORD)
_API_KEYS = (CONF_API_USERNAME, CONF_API_PASSWORD)
_LOGIN_KEYS = (*_API_KEYS, CONF_RTSP_USERNAME, CONF_RTSP_PASSWORD)
# Form fields of each section that the reconfigure form fills from the entry: never a password.
_RTSP_SHOWN = ((CONF_PORT, CONF_RTSP_PORT), (CONF_USERNAME, CONF_RTSP_USERNAME))
_API_SHOWN = ((CONF_USERNAME, CONF_API_USERNAME),)
# How long the trial check waits for a free check slot before the form reports busy checks.
SLOT_WAIT = 10


class _SlotBusy(Exception):
    """No check slot became free in time for the trial check."""


@asynccontextmanager
async def _trial_slot(hass: HomeAssistant) -> AsyncIterator[None]:
    """Take a check slot for the trial check, waiting at most `SLOT_WAIT` seconds.

    The running cameras share the slots, and each may hold one for up to its
    frame timeout, so without a limit the form could spin for minutes.

    Raises:
        _SlotBusy: No slot became free in time.
    """
    semaphore = probe_semaphore(hass)
    try:
        async with asyncio.timeout(SLOT_WAIT):
            await semaphore.acquire()
    except TimeoutError:
        raise _SlotBusy from None
    try:
        yield
    finally:
        semaphore.release()


def _url_schema(default: str) -> vol.Schema:
    selector = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
    return vol.Schema({vol.Required(CONF_URL, default=default): selector})


def _clean_url(raw: str) -> tuple[str | None, str | None]:
    """Return the base address of go2rtc and the form error key, if any.

    The query and the fragment are dropped, and so is a page name: an address
    copied from the go2rtc web UI looks like `http://host:1984/streams.html?src=x`.
    An API address is cut back the same way: `/api` and `/api/<one name>`, such
    as `/api/streams?src=x`; a longer path after `api` belongs to a proxy. Any
    other path stays, encoded as typed, because go2rtc may live behind a proxy
    under one.

    Input that is not an http or https URL gives `(None, "invalid_url")`. An
    address with a login gives `url_login` together with the address without
    it, which the form may show again: the API login has its own fields, and a
    typed password must never come back to the browser.
    """
    try:
        url = URL(cv.url(raw.strip()))
    except vol.Invalid, ValueError:
        return None, "invalid_url"
    error = None
    if url.user is not None or url.password is not None:
        url, error = url.with_user(None), "url_login"
    path = url.raw_path.rstrip("/")
    head, _, tail = path.rpartition("/")
    if tail.endswith(".html") or tail == "api":
        path = head
    elif head.rpartition("/")[2] == "api":
        path = head.rpartition("/")[0]
    return str(url.with_path(path, encoded=True)).rstrip("/"), error


def _login_fields() -> dict[vol.Marker, Any]:
    return {
        vol.Optional(CONF_USERNAME): TextSelector(),
        vol.Optional(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }


def _api_section() -> section:
    return section(vol.Schema(_login_fields()), {"collapsed": True})


def _rtsp_section() -> section:
    return section(
        vol.Schema({vol.Optional(CONF_PORT): _number(1, 65535), **_login_fields()}),
        {"collapsed": True},
    )


def _login(
    section_input: Mapping[str, Any],
    saved: Mapping[str, Any],
    typed: Mapping[str, Any],
    username_key: str,
    password_key: str,
    *,
    empty_password: bool = False,
) -> tuple[str, str] | None:
    """Return the username and password a section asks for, `None` if half filled.

    The form never shows a password, neither the saved one nor one typed before
    a form error, so an empty password keeps the typed one, else the saved one,
    while the username stays the same. Both empty mean no login.

    With `empty_password`, a username may also go without a password, but not
    in place of a saved password: there an empty field would drop that password
    for a new username, which more likely means it was forgotten.
    """
    username = section_input.get(CONF_USERNAME) or ""
    password = section_input.get(CONF_PASSWORD) or ""
    if username and not password:
        for source in (typed, saved):
            if username == source.get(username_key):
                return username, source.get(password_key, "")
        if empty_password and not saved.get(password_key):
            return username, ""
        return None
    if password and not username:
        return None
    return username, password


def _api_data(
    section_input: Mapping[str, Any], saved: Mapping[str, Any], typed: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Turn the API section into entry data and the form error key, if any.

    A password needs its username, and the username cannot hold a colon:
    Basic authentication splits the login at the first one, so go2rtc would
    never accept it. The password may be empty: go2rtc turns its login on with
    `api.username` alone and then takes the username with an empty password.
    An empty username drops the login.
    """
    login = _login(
        section_input, saved, typed, CONF_API_USERNAME, CONF_API_PASSWORD, empty_password=True
    )
    if login is None or ":" in login[0]:
        return {}, "api_login"
    username, password = login
    if not username:
        return {}, None
    return {CONF_API_USERNAME: username, CONF_API_PASSWORD: password}, None


def _rtsp_data(
    section_input: Mapping[str, Any], saved: Mapping[str, Any], typed: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Turn the RTSP section into entry data and the form error key, if any.

    The login must be filled in full and fit ffmpeg's limit. An empty username
    drops the login.
    """
    data: dict[str, Any] = {}
    if (port := section_input.get(CONF_PORT)) is not None:
        data[CONF_RTSP_PORT] = port
    login = _login(section_input, saved, typed, CONF_RTSP_USERNAME, CONF_RTSP_PASSWORD)
    if login is None:
        return data, "rtsp_login"
    username, password = login
    if username:
        data[CONF_RTSP_USERNAME] = username
        data[CONF_RTSP_PASSWORD] = password
        if len(rtsp_login(rtsp_access(data))) > MAX_RTSP_LOGIN:
            return data, "rtsp_login_long"
    return data, None


def _logins(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the API and RTSP logins of entry data, to keep on the flow between forms."""
    return {key: value for key, value in data.items() if key in _LOGIN_KEYS}


def _without_password(section_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return what a section shows again: everything but the password."""
    return {key: value for key, value in section_input.items() if key != CONF_PASSWORD}


def _stream_choices(streams: list[str]) -> dict[str, str]:
    """Map a unique safe label to each stream name.

    The label is also the option value, so a name that carries a password never
    reaches the browser. Streams named by address share one label, so a number
    tells them apart.
    """
    labels = [stream_label(name) for name in streams]
    repeated = {label for label in labels if labels.count(label) > 1}
    seen: dict[str, int] = {}
    choices: dict[str, str] = {}
    for name, label in zip(streams, labels, strict=True):
        if label in repeated:
            seen[label] = seen.get(label, 0) + 1
            label = f"{label} ({seen[label]})"
        choices[label] = name
    return choices


def _number(minimum: int, maximum: int) -> vol.All:
    selector = NumberSelector(
        NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX)
    )
    return vol.All(selector, vol.Coerce(int))


class FramePulseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Creates an entry: one entry is one camera."""

    VERSION = 1
    # 2 since 0.1.1: the monitoring flag moved from the entry data to the options.
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._url = DEFAULT_URL
        # The API login of the address step, as entry data.
        self._api: dict[str, Any] = {}
        # The logins of the last submission, as entry data. A form shown again after
        # an error never carries a password, so an empty one with the same username
        # takes it from here; it stays on the server, never in the form.
        self._typed: dict[str, Any] = {}
        self._streams: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(entry: FramePulseConfigEntry) -> FramePulseOptionsFlow:
        """Return the options flow."""
        return FramePulseOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the go2rtc address and API login, and fetch the stream list.

        The login is needed here already: the stream list comes from the API.
        """
        errors: dict[str, str] = {}
        suggested: dict[str, Any] | None = None
        if user_input is not None:
            url, url_error = _clean_url(user_input[CONF_URL])
            api, api_error = _api_data(user_input[CONF_API], {}, self._typed)
            self._typed = _logins(api)
            # An address with a login comes back without it. An invalid one comes back
            # as typed, unless it may hold a login (it has `@`): then the previous
            # address shows, and the password typed for another host is forgotten.
            suggested = {CONF_API: _without_password(user_input[CONF_API])}
            if url is not None:
                self._url = url
            elif "@" not in user_input[CONF_URL]:
                suggested[CONF_URL] = user_input[CONF_URL]
            else:
                self._typed = {}
            if url_error:
                errors[CONF_URL] = url_error
            elif api_error:
                errors["base"] = api_error
            else:
                try:
                    streams = await list_streams(
                        async_get_clientsession(self.hass), self._url, api=api_access(api)
                    )
                except Go2rtcAuthError:
                    # A refused password is not kept: an empty field then means none.
                    self._typed = {}
                    errors["base"] = "invalid_auth"
                except Go2rtcError:
                    errors["base"] = "cannot_connect"
                else:
                    self._streams = _stream_choices(streams)
                    if self._streams:
                        self._api = api
                        return await self.async_step_stream()
                    errors["base"] = "no_streams"
        schema = _url_schema(self._url).extend({vol.Required(CONF_API): _api_section()})
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_stream(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick a stream, a name and the RTSP settings, then run a trial check.

        A failed frame does not block saving: the camera may be off.
        Only an RTSP login that is incomplete or too long, no free check slot
        within `SLOT_WAIT` seconds, a broken check or an unknown stream does.
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        suggested: dict[str, Any] | None = None
        if user_input is not None:
            stream = self._streams[user_input[CONF_STREAM]]
            await self.async_set_unique_id(entry_unique_id(self._url, stream))
            self._abort_if_unique_id_configured()
            suggested = {**user_input, CONF_RTSP: _without_password(user_input[CONF_RTSP])}

            rtsp, rtsp_error = _rtsp_data(user_input[CONF_RTSP], {}, self._typed)
            self._typed = _logins(rtsp)
            if rtsp_error:
                errors["base"] = rtsp_error
            elif (result := await self._trial_check(stream, rtsp)) is None:
                errors["base"] = "checks_busy"
                placeholders["seconds"] = str(SLOT_WAIT)
            elif result.status is Status.PROBE_ERROR:
                # The API answered in the previous step, so the reason is usually
                # RTSP or ffmpeg; the scrubbed detail tells which, or names
                # HTTP 401 if the API login stopped working since.
                errors["base"] = "check_failed"
                placeholders["reason"] = result.detail
                # The RTSP login may be the reason, so it must be typed again.
                self._typed = {}
            elif result.status is Status.STREAM_NOT_FOUND:
                errors[CONF_STREAM] = "stream_not_found"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_URL: self._url, CONF_STREAM: stream, **self._api, **rtsp},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_STREAM): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=label, label=label) for label in self._streams
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_RTSP): _rtsp_section(),
            }
        )
        return self.async_show_form(
            step_id="stream",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _trial_check(self, stream: str, rtsp: Mapping[str, Any]) -> FrameResult | None:
        """Run the trial check of the stream step; `None` if no check slot became free."""
        try:
            return await probe_frame(
                async_get_clientsession(self.hass),
                self._url,
                stream,
                get_ffmpeg_manager(self.hass).binary,
                DEFAULT_FRAME_TIMEOUT,
                _trial_slot(self.hass),
                access=rtsp_access(rtsp),
                api=api_access(self._api),
            )
        except _SlotBusy:
            return None

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication: go2rtc refused the API login or asks for a missing one."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the go2rtc API login again and check it against go2rtc.

        The fields work as in the API section of the other forms, except that
        the saved password is never reused, since go2rtc refused it: an empty
        password means none (a username alone), or the one typed before a form
        error for the same username. An empty username drops the login. The
        saved go2rtc address is used; a new address goes through Reconfigure.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            saved = {k: v for k, v in entry.data.items() if k != CONF_API_PASSWORD}
            api, api_error = _api_data(user_input, saved, self._typed)
            self._typed = _logins(api)
            if api_error:
                # The shared text speaks of a saved password, which this form never reuses.
                errors["base"] = "reauth_api_login"
            else:
                try:
                    await list_streams(
                        async_get_clientsession(self.hass),
                        entry.data[CONF_URL],
                        api=api_access(api),
                    )
                except Go2rtcAuthError:
                    # A refused password is not kept: an empty field then means none.
                    self._typed = {}
                    errors["base"] = "invalid_auth"
                except Go2rtcError:
                    # This form shows no address, so the text names the saved one.
                    errors["base"] = "reauth_cannot_connect"
                else:
                    kept = {k: v for k, v in entry.data.items() if k not in _API_KEYS}
                    return self.async_update_reload_and_abort(entry, data={**kept, **api})
            suggested = _without_password(user_input)
        else:
            suggested = {key: entry.data[name] for key, name in _API_SHOWN if name in entry.data}
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(_login_fields()), suggested),
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the go2rtc address, API login and RTSP settings without a connection check.

        The connection is not checked on purpose: an unreachable go2rtc or a
        wrong RTSP login must show up as `probe_error` in the camera status,
        and a refused API login as a reauthentication request, not block
        saving. Errors are reported in the order of the form.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url, url_error = _clean_url(user_input[CONF_URL])
            api, api_error = _api_data(user_input[CONF_API], entry.data, self._typed)
            rtsp, rtsp_error = _rtsp_data(user_input[CONF_RTSP], entry.data, self._typed)
            self._typed = _logins({**api, **rtsp})
            if url_error:
                errors[CONF_URL] = url_error
            elif api_error or rtsp_error:
                errors["base"] = api_error or rtsp_error
            else:
                assert url is not None
                unique_id = entry_unique_id(url, entry.data[CONF_STREAM])
                other = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique_id)
                if other is not None and other.entry_id != entry.entry_id:
                    return self.async_abort(reason="already_configured")
                kept = {k: v for k, v in entry.data.items() if k not in (*_API_KEYS, *_RTSP_KEYS)}
                return self.async_update_reload_and_abort(
                    entry, unique_id=unique_id, data={**kept, CONF_URL: url, **api, **rtsp}
                )
            # What was typed comes back, not the saved values, but never a password:
            # an address with a login comes back without it, and an invalid one that
            # may hold a login (it has `@`) not at all, so the saved address shows.
            suggested = {
                CONF_API: _without_password(user_input[CONF_API]),
                CONF_RTSP: _without_password(user_input[CONF_RTSP]),
            }
            if url is not None:
                suggested[CONF_URL] = url
            elif "@" not in user_input[CONF_URL]:
                suggested[CONF_URL] = user_input[CONF_URL]
        else:
            suggested = {
                CONF_API: {key: entry.data[name] for key, name in _API_SHOWN if name in entry.data},
                CONF_RTSP: {
                    key: entry.data[name] for key, name in _RTSP_SHOWN if name in entry.data
                },
            }
        schema = _url_schema(entry.data[CONF_URL]).extend(
            {vol.Required(CONF_API): _api_section(), vol.Required(CONF_RTSP): _rtsp_section()}
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


class FramePulseOptionsFlow(OptionsFlowWithReload):
    """Options of the camera check; saving reloads the entry, no Home Assistant restart needed."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and validate the options."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # The selector cannot express "0 or 2-100": it accepts 1, which is not a valid value.
            if 0 < user_input[CONF_SAME_FRAMES] < MIN_SAME_FRAMES:
                errors[CONF_SAME_FRAMES] = "invalid_same_frames"
            else:
                # The monitoring flag of the switch is kept in the options too.
                return self.async_create_entry(data={**self.config_entry.options, **user_input})

        values = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=values.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): _number(MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL),
                vol.Required(
                    CONF_FRAME_TIMEOUT,
                    default=values.get(CONF_FRAME_TIMEOUT, DEFAULT_FRAME_TIMEOUT),
                ): _number(MIN_FRAME_TIMEOUT, MAX_FRAME_TIMEOUT),
                vol.Required(
                    CONF_FAILURES, default=values.get(CONF_FAILURES, DEFAULT_FAILURES)
                ): _number(MIN_FAILURES, MAX_FAILURES),
                vol.Required(
                    CONF_SAME_FRAMES, default=values.get(CONF_SAME_FRAMES, DEFAULT_SAME_FRAMES)
                ): _number(0, MAX_SAME_FRAMES),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
