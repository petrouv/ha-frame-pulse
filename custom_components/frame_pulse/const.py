"""Integration constants."""

from __future__ import annotations

DOMAIN = "frame_pulse"

DEFAULT_URL = "http://127.0.0.1:1984"

CONF_STREAM = "stream"
CONF_MONITORING = "monitoring"
CONF_FRAME_TIMEOUT = "frame_timeout"
CONF_FAILURES = "failures"
CONF_SAME_FRAMES = "same_frames"
# The go2rtc stream the snapshot action takes its frame from; an entry without it
# takes the monitored one.
CONF_SNAPSHOT_STREAM = "snapshot_stream"
# The form section with the go2rtc API login, and the entry data it fills.
# An entry without these keys reaches an API that asks for no login.
CONF_API = "api"
CONF_API_USERNAME = "api_username"
CONF_API_PASSWORD = "api_password"
# The form section with the go2rtc RTSP server settings, and the entry data it fills.
CONF_RTSP = "rtsp"
CONF_RTSP_PORT = "rtsp_port"
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"

DEFAULT_SCAN_INTERVAL = 60
DEFAULT_FRAME_TIMEOUT = 20
DEFAULT_FAILURES = 2
DEFAULT_SAME_FRAMES = 5

MIN_SCAN_INTERVAL = 15
MAX_SCAN_INTERVAL = 3600
MIN_FRAME_TIMEOUT = 5
MAX_FRAME_TIMEOUT = 60
MIN_FAILURES = 1
MAX_FAILURES = 10
MIN_SAME_FRAMES = 2
MAX_SAME_FRAMES = 100

# At most this many check ffmpeg runs at once across the whole integration, the
# setup form's trial check included; other checks wait for a slot.
MAX_CONCURRENT_PROBES = 2
# Snapshots have slots of their own, so a snapshot never waits behind checks of
# hanging cameras, and a burst of snapshots still starts a bounded number of ffmpeg runs.
MAX_CONCURRENT_SNAPSHOTS = 2

SERVICE_SNAPSHOT = "snapshot"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DEVICE_ID = "device_id"
ATTR_FILENAME = "filename"
ATTR_TIMEOUT = "timeout"
