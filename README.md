# Frame Pulse

A Home Assistant integration that checks all your cameras by one criterion: **does the camera actually return a fresh
frame?** At every interval it takes one keyframe of the stream from [go2rtc](https://github.com/AlexxIT/go2rtc) and
creates entities for each camera, so a single automation can notify you when a camera drops off, freezes or comes back.

The integration sends no notifications itself and does not change camera settings or `go2rtc.yaml`.

## Requirements

- Home Assistant 2026.10 or newer.
- go2rtc with your camera streams configured. The behavior was checked against go2rtc 1.9.9; other versions were
  not tested. Prefer low-resolution streams: every check decodes a frame with ffmpeg on the Home Assistant machine.
- A go2rtc instance that knows those streams. The go2rtc built into Home Assistant Core usually does not.
- The go2rtc API reachable from Home Assistant. A go2rtc API login (`api.username` and `api.password` in
  `go2rtc.yaml`, or `api.username` alone, which go2rtc takes with an empty password) is supported. go2rtc asks for it
  unless the request comes from 127.0.0.1 or ::1, and from there too if `api.local_auth: true` is set (go2rtc 1.9.12
  and newer). go2rtc goes by where the request comes from, not by the address typed: an address such as the LAN IP of
  the go2rtc machine or a Docker host name is checked even when Home Assistant runs on the same machine, and so is
  `127.0.0.1` on the host when go2rtc runs in a Docker container with the port published (`-p 1984:1984`), since the
  request then reaches go2rtc from the Docker network. When go2rtc does not ask (a request from its own 127.0.0.1 or
  ::1 without `local_auth`), it does not check the login either, so a wrong one is saved without an error and is
  noticed only after the address or `local_auth` changes: Home Assistant then asks for the login again, see
  [When go2rtc refuses the API login](#when-go2rtc-refuses-the-api-login). The API login is separate from the RTSP
  login below.
- The go2rtc RTSP server turned on (it is by default, port 8554) and reachable from Home Assistant. By default the
  check uses the port that go2rtc reports in its `rtsp.listen` setting. If go2rtc runs in Docker with the RTSP port
  published under another number, or the RTSP server asks for a login (`rtsp.username` and `rtsp.password` in
  `go2rtc.yaml`; go2rtc does not ask for them from the same machine, but asks from any other one), fill in the
  **go2rtc RTSP server** section of the form, see [Configuration](#configuration).
- ffmpeg, which every Home Assistant installation has (the built-in `ffmpeg` integration is loaded with this one).

### How a frame is taken

A check asks `GET /api` whether go2rtc is up and, unless the port is set in the entry, which RTSP port it uses, then
runs ffmpeg against the go2rtc RTSP address of the stream and reads the first keyframe as JPEG from its output. go2rtc's own `/api/frame.jpeg` is not used: a request to a
camera that connects but sends no frames hangs there, and go2rtc keeps a consumer for every such request and keeps
reconnecting to the camera every 5 seconds. ffmpeg is killed when the frame timeout runs out, and go2rtc drops its
session at once.

While Home Assistant starts, the first check and the regular checks wait until startup is over: a go2rtc that another
integration starts (for example WebRTC Camera) may not answer yet, and the camera would show `probe_error` until the next
check. Monitoring turned on during startup waits as well. The **Check now** button is the exception: it checks at once,
since it is pressed on purpose. An entry added or reloaded later is checked at once.

## Installation

1. Open [this My Home Assistant link](https://my.home-assistant.io/redirect/hacs_repository/?owner=petrouv&repository=ha-frame-pulse&category=integration).
   It opens HACS on Home Assistant before 2026.11 and the Marketplace on 2026.11 and newer. Since the link names the
   category, it offers to add the repository as a custom repository: it is not in the default catalog yet. Download
   the integration there, then restart Home Assistant.
2. Without HACS or the Marketplace, copy `custom_components/frame_pulse` from the
   [latest release](https://github.com/petrouv/ha-frame-pulse/releases/latest) to `config/custom_components/frame_pulse`
   and restart Home Assistant.
3. Go on with [Configuration](#configuration).

## Configuration

**Settings → Devices & services → Add integration → Frame Pulse.** One entry is one camera: the go2rtc address
(`http://127.0.0.1:1984` by default), a stream from the list and a name. One trial check runs when the entry is created.
A camera is the go2rtc instance plus the stream, so the same stream name on another go2rtc is added as another
camera. The address may be pasted from the go2rtc web UI or from an API request: the page name, `/api` with the name
after it (as in `/api/streams?src=cam`) and the query are dropped. Any other path is kept as typed, `%2F` included,
since go2rtc may live behind a proxy under it.

The address step has a collapsed **go2rtc API login** section, needed only if go2rtc asks for `api.username` and
`api.password` (see [Requirements](#requirements)). A password needs its username; the password may stay empty if
go2rtc sets `api.username` without one. The username cannot contain a colon, since go2rtc could never accept it. An address with a login in it (`http://user:pass@host:1984`) is refused: put the login in
this section instead. If go2rtc refuses the login, the form says so and keeps the username, never the password. After any other error of the
form, an empty password field keeps the password typed before as long as the username stays the same, except after an
invalid address with `@` in it: the last valid address (or the default) comes back and the password must be typed again. The typed
password is held by Home Assistant for this form only and never sent back to the browser.

The camera step has a collapsed **go2rtc RTSP server** section, needed only in two cases:

- **RTSP port**: go2rtc runs in Docker and publishes its RTSP server under another number, as `-p 18554:8554`. Enter
  the published port (18554 here). Empty means the port go2rtc reports in `rtsp.listen`. A set port is used as is,
  even if go2rtc reports its RTSP server off. It must be the RTSP port: a port of another service, such as the API port
  1984, answers something that is not RTSP, and the camera shows `no_stream` instead of an error.
- **RTSP username** and **RTSP password**: `rtsp.username` and `rtsp.password` from `go2rtc.yaml`. Fill in both or
  neither. ffmpeg cuts a login longer than 127 characters once encoded (a symbol takes 3 characters, a Cyrillic letter 6), so the
  form refuses one: the username and password together fit only about 40 symbols or 20 Cyrillic letters.

The form shows an error if go2rtc is unreachable or refuses the API login, the stream is not found, or the trial
check is broken: the go2rtc RTSP
server is off or unreachable, does not answer on the RTSP port, rejects the login, or ffmpeg cannot start. The error
names the reason, and the form keeps what was typed except the password. An empty RTSP password field then keeps the
password typed before for the same username, except after a failed trial check, where it must be typed again. A failed frame does not block saving: the
camera may be off. To add a camera in planned shutdown, create the entry and then turn **Monitoring** off; the camera
gets the trial check and the first scheduled check before that.

The go2rtc address, the API login section and the RTSP section can be changed with **Reconfigure** on the entry. The
saved API and RTSP passwords are not shown there: an empty password field keeps the saved one as long as the username
stays the same, a new username needs its password, and an empty username removes the login. After a form error, an
empty password field keeps the password typed before the error instead, for the same username. For the API login only,
a new username with an empty password is accepted when no API password is saved, for go2rtc set up with `api.username`
alone. To drop a saved API password but keep the username, clear the username and save, then open **Reconfigure** again
and enter the username alone. The connection is not
checked there on purpose: if go2rtc is unreachable or the RTSP settings are wrong, the camera status becomes
`probe_error`; if go2rtc refuses the API login, Home Assistant asks for it again (see
[When go2rtc refuses the API login](#when-go2rtc-refuses-the-api-login)).

Options apply without a Home Assistant restart (the entry reloads, the camera state is kept):

| Option | Default | Range |
|---|---|---|
| Check interval | 60 s | 15–3600 s |
| Frame timeout | 20 s | 5–60 s |
| Failures in a row before "disconnected" | 2 | 1–10 |
| Identical frames in a row before "frozen" | 5 | 2–100; 0 turns the check off |

At most 2 ffmpeg runs go at the same time across the whole integration, including the trial check of the setup form. Any further check waits for a free slot, and the wait does not count as response time.
The trial check waits at most 10 seconds for a free slot: if none becomes free by then, the form says that other cameras are being
checked, and the form can be sent again a little later.

### When go2rtc refuses the API login

If go2rtc refuses the saved API login during a check (HTTP 401 from `GET /api` or `GET /api/streams`), the camera does
not get the status `probe_error`. Home Assistant logs one error, stops checking that camera and asks for the login
again under **Settings → Devices & services**. Until then the entities of the camera are `unavailable`, **Check now**
included, except the Monitoring switch: it stays usable, so a planned shutdown can be marked meanwhile. Turning it off
shows `disabled`, as in any planned shutdown. Turning it back on runs one check; if go2rtc still refuses the login,
checking stops again and no second request is opened (a dismissed one is opened again), and if go2rtc takes the saved
login again, the checks resume, while the open request stays listed until it is answered or dismissed.
The form asks for the API
username and password with the same rules as the **go2rtc API login** section, except that a saved password is never
reused, since go2rtc refused it: an empty password field means no password, or after a form error the password typed
before for the same username, unless go2rtc refused that login. The form tries the login against go2rtc, saves it and
reloads the entry, and the checks resume. If go2rtc refuses the new login or does not answer, the form says so.
The form uses the saved go2rtc address; if go2rtc has moved, change the address with **Reconfigure** instead, which also
ends the request.
Each camera added from the same go2rtc asks on its own. A refused RTSP login (ffmpeg's `401 Unauthorized`) is still
`probe_error`.

## Statuses

| Status | Condition | Meaning |
|---|---|---|
| `ok` | A complete JPEG from 128 bytes to 10 MB | The camera works |
| `frozen` | The same frame received N times in a row | The picture is frozen |
| `no_stream` | go2rtc cannot reach the camera (RTSP 404 for a stream that exists), or ffmpeg ends without a frame | The camera returns no stream |
| `bad_frame` | ffmpeg returns data that fails validation | The camera or the stream is faulty |
| `frame_timeout` | No frame within the frame timeout: the camera connects but sends no picture | The camera hangs |
| `stream_not_found` | RTSP 404, and the stream is not in go2rtc's stream list | Configuration error or an edit in `go2rtc.yaml` |
| `probe_error` | `GET /api` (or `GET /api/streams` after RTSP 404) does not answer as go2rtc (also after a failed frame), the RTSP server is off or unreachable, refuses the connection, does not answer on its port, or rejects the login (none or a wrong one), or ffmpeg cannot start. A refused API login is not a status: Home Assistant asks for it again, see [When go2rtc refuses the API login](#when-go2rtc-refuses-the-api-login) | The check is broken, not the camera |
| `disabled` | Monitoring is off | Planned shutdown |

## Entities

| Entity | Type | Meaning |
|---|---|---|
| Connection | `binary_sensor`, `connectivity` | `on`: the camera returns a frame, even a frozen one |
| Frozen | `binary_sensor`, `problem` | `on`: the picture is frozen. A failed check in between does not turn it off; a new frame does (for a changed freeze threshold see [Limitations](#limitations)) |
| Status | `sensor`, `enum` | Values from the table above |
| Picture last changed | `sensor`, `timestamp`, diagnostic | Time when the picture last changed, survives a restart |
| Response time | `sensor`, ms, diagnostic | How long ffmpeg ran in the last check, also when it got no frame; unknown if ffmpeg did not run |
| Monitoring | `switch`, configuration | Planned shutdown |
| Check now | `button` | Out-of-schedule check. Returns an error while monitoring is off, when the entry is reloaded during the check, and when go2rtc refuses the API login during the check |

The status has an icon for each value; **Monitoring** and **Check now** have their own icons too.

With `probe_error` and `disabled` the binary sensors are `unavailable`. The status stays available, so it shows when the
check itself is broken.

The connection and the freeze survive a Home Assistant restart and a reload of the entry, together with the failure
count and the identical frame count. Until the first check after them, the binary sensors show their last value and the
status is `unknown`; the first check goes on from that value. While monitoring is off, none of this is kept; only
**Picture last changed** keeps its value.

**Planned shutdown.** While monitoring is off, no requests go to the camera. A check already running when monitoring
is turned off finishes (up to the frame timeout) and its result is discarded. The state survives a restart: it is kept
in the entry options (`monitoring` under `options` in diagnostics), and an entry of 0.1.0, which kept it in the entry
data, has it moved there on the first start after the update. Going back to 0.1.0 keeps the entries working but turns
monitoring back on for every camera. After updating again, a change of the **Monitoring** switch made under 0.1.0 is
lost and the state from before the rollback comes back. Turning monitoring on starts a check right away; the camera state is unknown until
it ends. The integration never turns monitoring off by itself, even if a camera stays unreachable for a long time.

The Monitoring switch is a configuration entity, and Home Assistant leaves those out when an action targets a device,
an area or a floor. To turn monitoring off from a script, name the switch by its entity ID or by a label.

## Notifications

An example automation: one notification each when a camera is lost, freezes or comes back, when `probe_error` lasts
longer than 10 minutes, and when go2rtc no longer has the stream. Entity IDs depend on the camera name and the Home Assistant language, so take them from the
device page. Replace the `notify` action, `url`, `clickAction` and `group` with your own.

```yaml
alias: Frame Pulse
mode: parallel
triggers:
  - trigger: state
    entity_id: &connection
      - binary_sensor.driveway_connection
      - binary_sensor.garage_connection
    from: "on"
    to: "off"
    id: lost
  - trigger: state
    entity_id: &frozen
      - binary_sensor.driveway_frozen
      - binary_sensor.garage_frozen
    from: "off"
    to: "on"
    id: frozen
  - trigger: state
    entity_id: *connection
    from: "off"
    to: "on"
    id: back
  - trigger: state
    entity_id: *frozen
    from: "on"
    to: "off"
    id: back
  - trigger: state
    entity_id:
      - sensor.driveway_status
      - sensor.garage_status
    to: probe_error
    for:
      minutes: 10
    id: probe_error
  - trigger: state
    entity_id:
      - sensor.driveway_status
      - sensor.garage_status
    to: stream_not_found
    id: stream_not_found
actions:
  - action: notify.mobile_app_my_phone
    data:
      title: >-
        {{ device_attr(trigger.entity_id, 'name_by_user')
           or device_attr(trigger.entity_id, 'name') }}
      message: >-
        {{ {'lost': 'Camera connection lost',
            'frozen': 'Picture is frozen',
            'back': 'Camera is working again',
            'probe_error': 'Camera check is broken: check go2rtc, its RTSP settings and ffmpeg',
            'stream_not_found': 'go2rtc has no such stream: check go2rtc.yaml'}[trigger.id] }}
      data:
        url: /lovelace/cameras
        clickAction: /lovelace/cameras
        tag: "frame-pulse-{{ device_id(trigger.entity_id) }}"
        group: cameras
```

Triggers with `from` prevent false notifications: the binary sensors are `unavailable` during `probe_error`, while
monitoring is off and while Home Assistant asks for the API login, and `unknown` until the first frame, and the
automation does not react to those transitions. A restart or a reload of the entry does not get in the way: the
sensors keep their value across it, so the count goes on as if nothing happened. A camera that came back meanwhile
sends its notification after the first check, and one that died sends it after the set number of failed checks in a
row (two by default). The price:

- If a camera died or came back while its sensors were `unavailable` or `unknown`, you get no notification, and the
  fact is visible only in the status. This happens during a period of `probe_error` and after turning monitoring back
  on.
- If a picture froze before a planned shutdown, the freeze notification comes again after turning monitoring back on,
  because counting identical frames starts over.
- The `stream_not_found` and `probe_error` triggers have no `from`, so while the problem lasts their notification
  comes again after a reload of the entry, after turning monitoring back on and sometimes after a Home Assistant
  restart (`probe_error` 10 minutes later).
- A frozen camera that is lost and then comes back sends "Camera is working again" twice: the connection turns `on`
  and the freeze turns `off` with the same new frame. Both carry the same `tag`, so on a phone the second replaces the
  first.
- A refused go2rtc API login is not `probe_error`, so the `probe_error` trigger does not fire for it; Home Assistant
  asks for the login itself, see [When go2rtc refuses the API login](#when-go2rtc-refuses-the-api-login).

## Limitations

- **A freeze is detected only by an exact SHA-256 match of the whole JPEG.** Sensor noise and the on-screen clock usually
  change the bytes, so a match means the frame really repeats, and a still scene raises no alarm. A camera that
  re-encodes a frozen picture **will not be caught** by this check.
- The freeze threshold counts frames, not repeats: a value of 5 means the first frame plus four identical ones. A failed
  check does not break the series.
- Changing the freeze threshold while a picture is frozen keeps the count of identical frames. The next identical frame
  adds one to it and is compared with the new value: if the count is still below the value, or the value is 0, the
  freeze turns `off` (until then it stays `on`), which the example automation reports as "Camera is working again"
  although the picture is still frozen. A raised value turns it `on` again once the count reaches it, with a second
  freeze notification.
- The time of the last frame does not move while the same frame keeps coming.
- `no_stream`, `bad_frame` and `frame_timeout` count as failures towards the "disconnected" threshold.
- After any failed frame, go2rtc is asked again: `GET /api`, or `GET /api/streams` after RTSP 404. If go2rtc does not
  answer properly then, the result is `probe_error`, not a camera failure: another service on the port could have
  produced the failure too. The reason of the failed frame stays in the log line, for example
  `go2rtc API failed: no response (after no frame in 20 s)`. If go2rtc refuses the API login then, Home Assistant asks
  for the login instead.
  While `/api` keeps failing, the connection sensor stays unavailable and does not turn `off`; use a `probe_error`
  notification for this case.
- After a frame timeout the RTSP port is also tried with a plain TCP connection (up to 5 s), closed at once. A port
  that does not answer at all, as one dropped by a firewall, keeps ffmpeg waiting just like a camera that sends no
  picture, so the result is `probe_error`, for example
  `the go2rtc RTSP port does not answer: no answer in 5 s (after no frame in 20 s)`. A refused port is `probe_error`
  right away, without this try. A port that accepts the connection and then stays silent, such as one forwarded to a
  hung process, passes this try, so a wrong RTSP port of that kind shows as `frame_timeout`, a camera failure.
- With `stream_not_found` the connection sensor keeps its previous value, because nothing is known about the camera.
  Only the status reports it, so a notification needs its own trigger, as in the example above.
- Both thresholds count checks, not time. An out-of-schedule check (the **Check now** button, `homeassistant.update_entity`,
  turning monitoring on) counts like a scheduled one, so it brings "disconnected" and "frozen" closer. Pressing
  **Check now** while the check of an earlier press still runs starts no new check: the press waits for that one and
  gets its result. After the Monitoring switch changes, a press starts a new check.
- The first check after Home Assistant starts is not repeated early. If go2rtc is not up yet, the status is
  `probe_error` until the next scheduled check, one minute with the default interval.
- A camera is identified by the go2rtc host, port and path (letter case in the host and an explicitly typed `:80` for
  http or `:443` for https do not matter). The same go2rtc reached by IP address and by host name
  counts as two different instances, so the same stream can be added twice that way.
- A frame may end with up to 30 bytes of `00`, `CR` or `LF` after the JPEG end marker. ffmpeg writes no padding; the
  margin is kept for safety.

## Troubleshooting

| Status | What to check |
|---|---|
| `probe_error` | Open `http://<go2rtc>:1984/api` in a browser (it asks for the API login if go2rtc has one): it must return JSON with `version` and `rtsp.listen`. The RTSP server must be on and reachable from Home Assistant on that port, or on the port set in **Reconfigure** (go2rtc in Docker). ffmpeg's `401 Unauthorized` in the reason means the RTSP login is missing or wrong; a missing or wrong go2rtc API login is not `probe_error`, see [When go2rtc refuses the API login](#when-go2rtc-refuses-the-api-login). Without a reply, check the address in **Reconfigure** |
| `stream_not_found` | The stream name in `go2rtc.yaml` was changed or removed. Add the camera again with the new name |
| `no_stream` | go2rtc cannot open the camera: check the camera, its power and the source line in `go2rtc.yaml`. Opening the stream in the go2rtc web UI usually shows the reason. If the stream plays there, check the RTSP port set in **Reconfigure**: `Invalid data found` in the Home Assistant log line of the check means it belongs to another service |
| `frame_timeout` | The camera connects but sends no picture within the frame timeout. Reboot the camera; raise the frame timeout if it is slow to start |
| `bad_frame` | ffmpeg returned something that is not a JPEG. Try the stream in the go2rtc web UI |

The reason of each status change is logged at INFO by `custom_components.frame_pulse`, for example
`Driveway: status no_stream, connected False (ffmpeg 1: ...)`. To see every check, set the logger to debug:

```yaml
logger:
  logs:
    custom_components.frame_pulse: debug
```

Diagnostics of a camera are downloaded from its device page (**⋮ → Download diagnostics**).

## Security

`GET /api/streams` returns camera addresses with usernames and passwords. The integration reads only stream names from
that response, and never logs, stores or exposes the response itself.

go2rtc names a stream after its source when the stream is requested by address, and a source can carry a secret in the
login, the query, the path or command arguments. So a stream name that looks like a source (it starts with a scheme
such as `rtsp:`, `ffmpeg:` or `ring:`, or contains `@`, `?`, `=`, `&`, `://` or a space) is never shown as is: the
setup list, the device model and diagnostics show only its scheme, as `rtsp://***`, or just `***`. Several such streams
in the setup list get a number, as `rtsp://*** (2)`. Log lines name the camera by its entry title. Inside error texts
every `rtsp://` address and any URL with a login or a query are masked too (the go2rtc address stays readable), and each
logged reason is cut to 200 characters.

Diagnostics contain the entry data (the go2rtc address, the API username and password and the RTSP username and
password redacted, the stream name in
the safe form above), the options, the status, counters and times. Frames are not written to disk or to history. The
SHA-256 digest of the last frame is kept only in the saved state Home Assistant restores after a restart
(`.storage/core.restore_state`), so a freeze survives the restart; it is not in history or diagnostics.

The entry's unique ID holds the go2rtc host, port and path and the stream name, never a login. A stream name that looks like a source
is replaced there by its SHA-256, so Home Assistant cannot print a secret from it in its log.

The RTSP login is kept in the entry like any other entry data, in `.storage` of Home Assistant. The password is never
shown in a form, and neither the username nor the password in a log or diagnostics; the username is shown in the form
again, like the port. Both are part of the RTSP address that ffmpeg gets on its command line, so other processes on the
Home Assistant machine can see them while a check runs.

The go2rtc API login is kept in the entry the same way and is never shown in a form (except the username), a log or
diagnostics. Unlike the RTSP login, it is not on the ffmpeg command line: it goes only in the `Authorization` header of
the requests to the go2rtc address. go2rtc uses HTTP Basic authentication, which anyone on the network can read over
plain `http`, so use `https` or a trusted network.

## Removal

1. **Settings → Devices & services → Frame Pulse**: delete every camera entry. Its device and entities go with it.
2. Remove the integration itself: uninstall **Frame Pulse** where it was installed from (HACS or the Marketplace), or
   delete `config/custom_components/frame_pulse` if it was copied by hand (see [Installation](#installation)).
3. Restart Home Assistant.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
