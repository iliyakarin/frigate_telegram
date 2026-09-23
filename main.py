"""
Frigate-Telegram Bot — Python 3.11+
Polls the Frigate HTTP API for detection events and sends rich video/photo
notifications to Telegram with event details in the caption.
"""

import asyncio
import html
import json
import math
import logging
import os
import signal
import sys
import time
import urllib.parse
from datetime import datetime, time as dt_time, timezone
from functools import wraps
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from camera_health import (
    CacheStorageMonitor,
    CameraHealthMonitor,
    HealthAlert,
    fetch_log_error_detail,
    format_downtime,
    parse_monitored_cameras,
)
from grouping import PendingGroup, merge_into_pending, split_ready_groups
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, BotCommand
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

# Load optional .env file for local development
load_dotenv()


# ─────────────────────────── Configuration ───────────────────────────

FRIGATE_URL = os.environ.get("FRIGATE_URL", "").rstrip("/")
FRIGATE_USERNAME = os.environ.get("FRIGATE_USERNAME")
FRIGATE_PASSWORD = os.environ.get("FRIGATE_PASSWORD")

# External URL for public event links (e.g. via Cloudflare Tunnel)
EXTERNAL_URL = os.environ.get("EXTERNAL_URL", "").rstrip("/")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Helper for safe integer environment variables with validation
def get_int_setting(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid value for %s: '%s'. Using default: %s", key, val, default)
        return default

# Helper for safe boolean environment variables
def get_bool_setting(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes", "on")


def parse_hhmm(raw: str, default: str) -> dt_time:
    """Parse an 'HH:MM' string into a time object, falling back to *default* on invalid input."""
    try:
        return datetime.strptime(raw.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        logger.warning("Invalid HH:MM value '%s'; using default: %s", raw, default)
        return datetime.strptime(default, "%H:%M").time()


def mask_url(url: str) -> str:
    """Mask a URL to show only the scheme and host, removing credentials and path."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}"
    except Exception:
        return "[redacted]"


MONITOR_CONFIG_RAW = os.environ.get("MONITOR_CONFIG", "")
HEALTH_MONITOR_CAMERAS_RAW = os.environ.get("HEALTH_MONITOR_CAMERAS", "")
HEALTH_MONITOR_CAMERAS = parse_monitored_cameras(HEALTH_MONITOR_CAMERAS_RAW)
camera_health_monitor = CameraHealthMonitor(monitored_cameras=HEALTH_MONITOR_CAMERAS, debounce_seconds=60)
# Alert when Frigate's /tmp/cache usage >= this % (clamped to 1..100): a full
# cache means the recording maintainer stalled and recordings aren't saved.
HEALTH_CACHE_THRESHOLD_PCT = min(100, max(1, get_int_setting("HEALTH_CACHE_THRESHOLD_PCT", 85)))
cache_health_monitor = CacheStorageMonitor(monitored_cameras=None, debounce_seconds=60)

# Night-only alerts: cameras in this set only notify inside the configured window.
# Empty set (unset/empty env var) means the feature is off — no camera is restricted.
NIGHT_ALERT_CAMERAS = set(parse_monitored_cameras(os.environ.get("NIGHT_ALERT_CAMERAS", "")) or [])

POLLING_INTERVAL = get_int_setting("POLLING_INTERVAL", 60)

TIMEZONE = os.environ.get("TIMEZONE", "UTC")
LOCALES = os.environ.get("LOCALES", "en-US")
DEBUG = get_bool_setting("DEBUG", False)



STATE_FILE = Path(os.environ.get("STATE_FILE", "/app/data/state.json"))

# Media fetching settings
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds between retry attempts
FRIGATE_TIMEOUT = get_int_setting("FRIGATE_TIMEOUT", 15)  # seconds for Frigate API requests
TELEGRAM_CONNECT_TIMEOUT = get_int_setting("TELEGRAM_CONNECT_TIMEOUT", 15)  # seconds for Telegram connection
UPLOAD_TIMEOUT = get_int_setting("UPLOAD_TIMEOUT", 60)  # seconds for Telegram media upload (tunnel-safe)

# Event grouping settings — merge rapid-fire review items on the same camera
# into a single notification so one physical event doesn't fragment into
# several short, out-of-order clips.
EVENT_MERGE_GAP = get_int_setting("EVENT_MERGE_GAP", 45)  # seconds of quiet before finalizing a group
MAX_EVENT_SPAN = get_int_setting("MAX_EVENT_SPAN", 300)  # hard cap on merged-group duration
CLIP_PADDING_SECONDS = get_int_setting("CLIP_PADDING_SECONDS", 5)  # extra seconds shown before/after the detected activity
MAX_TELEGRAM_FILE_SIZE = get_int_setting("MAX_TELEGRAM_FILE_SIZE", 50 * 1024 * 1024)  # Telegram bot upload limit (50 MB)
MAX_CLIP_PARTS = 10  # an oversized clip sends at most this many parts; the rest are dropped with a "truncated" note
# Parts can exceed the pro-rata size: Frigate stream-copies and cuts on keyframes with
# whole-second inpoints. Observed ~5% overshoot at the real 50 MB limit on 4K HEVC
# (47.3 MB part vs a 45 MB target at 0.9), so leave a wider margin.
CLIP_SPLIT_SAFETY = 0.8

# Shared Telegram API timeout kwargs for consistent usage across all media/message sends
TELEGRAM_TIMEOUT_KWARGS = {
    "read_timeout": UPLOAD_TIMEOUT,
    "write_timeout": UPLOAD_TIMEOUT,
    "connect_timeout": TELEGRAM_CONNECT_TIMEOUT,
}



# Media types configuration: { key: (filename, content_type) }
EVENT_MEDIA_CONFIG = {
    "clip": ("clip.mp4", "video/mp4"),
    "thumbnail": ("thumbnail.jpg", "image/jpeg"),
    "snapshot": ("snapshot.jpg", "image/jpeg"),
    "gif": ("preview.gif", "image/gif"),
}

# ─────────────────────────── Logging ─────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("frigate-telegram")

# Suppress noisy third-party loggers unless in debug mode
if not DEBUG:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

# ─────────────────────── Monitor Config Parser ───────────────────────


def parse_monitor_config(raw: str) -> dict[str, set[str]]:
    """Parse MONITOR_CONFIG env var into a camera→zones mapping.

    Format:  camera1:zone_a,zone_b;camera2:all
    Returns: {"camera1": {"zone_a", "zone_b"}, "camera2": {"all"}}

    If the string is empty, returns an empty dict (= monitor everything).
    """
    raw = raw.strip()
    if not raw:
        return {}

    config: dict[str, set[str]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue

        camera_part, sep, zones_part = entry.partition(":")
        camera = camera_part.strip()
        if not camera:
            continue

        if sep:
            zones = {z.strip() for z in zones_part.split(",") if z.strip()}
            config[camera] = zones if zones else {"all"}
        else:
            # Camera name without zones → monitor all zones
            config[camera] = {"all"}
    return config


MONITOR_CONFIG = parse_monitor_config(MONITOR_CONFIG_RAW)

NIGHT_ALERT_START = parse_hhmm(os.environ.get("NIGHT_ALERT_START", "22:00"), "22:00")
NIGHT_ALERT_END = parse_hhmm(os.environ.get("NIGHT_ALERT_END", "06:00"), "06:00")

# ─────────────────────── Notification State ──────────────────────────


class NotificationState:
    """Persist notification enabled/disabled state to a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._enabled: bool = True
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self._enabled = data.get("enabled", True)
                logger.info("Loaded notification state: %s", "enabled" if self._enabled else "disabled")
        except Exception:
            logger.warning("Could not load state file; defaulting to enabled")
            self._enabled = True

    async def _save(self) -> None:
        def _do_save():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps({"enabled": self._enabled}))
            tmp_path.replace(self._path)

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _do_save)
        except Exception:
            logger.warning("Could not persist state file")

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def enable(self) -> None:
        self._enabled = True
        await self._save()

    async def disable(self) -> None:
        self._enabled = False
        await self._save()


state = NotificationState(STATE_FILE)

# ─────────────────────── Frigate HTTP Client ─────────────────────────


def _http_auth() -> httpx.BasicAuth | None:
    if FRIGATE_USERNAME and FRIGATE_PASSWORD:
        return httpx.BasicAuth(FRIGATE_USERNAME, FRIGATE_PASSWORD)
    return None


async def check_frigate_status(client: httpx.AsyncClient) -> bool:
    """Return True if Frigate is reachable."""
    try:
        resp = await client.get(f"{FRIGATE_URL}/api/version", auth=_http_auth(), timeout=FRIGATE_TIMEOUT)
        resp.raise_for_status()
        logger.info("Frigate is up — version: %s", resp.text.strip())
        return True
    except Exception as exc:
        logger.error("Cannot reach Frigate at %s: %s", mask_url(FRIGATE_URL), exc)
        return False


async def fetch_review_items(client: httpx.AsyncClient, after_ts: float) -> list[dict]:
    """Fetch review items from Frigate API since *after_ts*.

    Review items cluster rapid-fire object-detection events on the same
    camera into one continuous activity segment server-side, which is what
    the polling loop groups notifications around. Unlike /api/events, a
    single call already covers all cameras.
    """
    try:
        resp = await client.get(
            f"{FRIGATE_URL}/api/review",
            params={"after": after_ts},
            auth=_http_auth(),
            timeout=FRIGATE_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Error fetching review items: %s", exc)
        return []


async def fetch_media_with_retry(
    client: httpx.AsyncClient,
    url: str,
    label: str,
    expected_content_type: str | None = None,
    max_retries: int = MAX_RETRIES,
) -> bytes | None:
    """Fetch media from a URL with retry logic for 404/transient errors.

    Args:
        url: Full URL to fetch.
        label: Human-readable label for logging (e.g. 'preview.gif for event X').
        expected_content_type: If set, warn when the response Content-Type
            doesn't match (helps detect octet-stream issues).
        max_retries: Maximum number of attempts.

    Returns:
        Raw bytes or None if all retries failed.
    """
    expected_ct_lower = expected_content_type.lower() if expected_content_type else None

    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.get(url, auth=_http_auth(), timeout=FRIGATE_TIMEOUT)

            if DEBUG:
                logger.debug("Fetching Frigate API: %s %s", resp.status_code, mask_url(url))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                # 404 means Frigate hasn't generated the media yet — retry
                if attempt < max_retries:
                    logger.debug("%s: media not ready (404), retry %d/%d", label, attempt, max_retries)
                else:
                    logger.warning("%s: media not found (404) after %d attempts. URL: %s", label, max_retries, mask_url(url))
            else:
                logger.error("%s: HTTP error %d: %s. URL: %s", label, exc.response.status_code, exc, mask_url(url))
            
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
        except httpx.RequestError as exc:
            logger.error("%s: Network error: %s", label, exc)
            if attempt < max_retries:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
        except Exception as exc:
            logger.error("%s: Unexpected error fetching %s: %s", label, mask_url(url), exc)
            return None

        # Success path
        try:
            # Verify basic response validity
            if len(resp.content) < 100:
                logger.warning("%s: response too small (%d bytes), retrying", label, len(resp.content))
                if attempt < max_retries:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return None

            # Verify content type if expected
            ct = resp.headers.get("content-type", "").lower()
            if expected_ct_lower and expected_ct_lower not in ct:
                logger.warning("%s: expected %s, got %s", label, expected_content_type, ct)

            logger.debug("Fetched %s: %d bytes", label, len(resp.content))
            return resp.content
        except Exception as exc:
            logger.error("%s: Error processing response: %s", label, exc)
            return None

    return None


async def _fetch_frigate_api(
    client: httpx.AsyncClient,
    path: str,
    label: str,
    expected_content_type: str | None = None,
    max_retries: int = MAX_RETRIES,
) -> bytes | None:
    """Internal helper to fetch from Frigate API."""
    url = f"{FRIGATE_URL}/api/{path}"
    return await fetch_media_with_retry(client, url, label, expected_content_type, max_retries=max_retries)


async def fetch_event_details(client: httpx.AsyncClient, event_id: str) -> dict | None:
    """Fetch full event details from Frigate API."""
    try:
        safe_event_id = urllib.parse.quote(event_id, safe='')
        resp = await client.get(
            f"{FRIGATE_URL}/api/events/{safe_event_id}",
            auth=_http_auth(),
            timeout=FRIGATE_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Error fetching event details for %s: %s", event_id, exc)
        return None


async def fetch_event_media(
    client: httpx.AsyncClient,
    event_id: str,
    media_type: Literal["gif", "clip", "thumbnail", "snapshot"],
    max_retries: int = MAX_RETRIES,
) -> bytes | None:
    """Fetch event-related media (gif, clip, or thumbnail)."""
    safe_event_id = urllib.parse.quote(event_id, safe='')
    filename, content_type = EVENT_MEDIA_CONFIG[media_type]
    return await _fetch_frigate_api(
        client,
        f"events/{safe_event_id}/{filename}",
        f"{filename} for {event_id}",
        content_type,
        max_retries=max_retries,
    )




async def fetch_camera_snapshot(
    client: httpx.AsyncClient, camera: str, max_retries: int = MAX_RETRIES
) -> bytes | None:
    """Fetch the latest snapshot JPEG from a camera."""
    safe_camera = urllib.parse.quote(camera, safe='')
    return await _fetch_frigate_api(
        client,
        f"{safe_camera}/latest.jpg?bbox=1",
        f"latest.jpg for {camera}",
        "image/jpeg",
        max_retries=max_retries,
    )


_CACHED_CAMERAS: list[str] | None = None
_CACHED_CAMERAS_TIMESTAMP: float = 0
_CAMERA_CACHE_TTL: int = 300


async def fetch_camera_list(client: httpx.AsyncClient) -> list[str]:
    """Fetch the list of camera names from Frigate API.
    Result is cached with a TTL.
    """
    global _CACHED_CAMERAS, _CACHED_CAMERAS_TIMESTAMP
    now = time.time()
    if _CACHED_CAMERAS is not None and (now - _CACHED_CAMERAS_TIMESTAMP) < _CAMERA_CACHE_TTL:
        return _CACHED_CAMERAS

    try:
        # /api/config contains the full configuration including cameras
        resp = await client.get(f"{FRIGATE_URL}/api/config", auth=_http_auth(), timeout=FRIGATE_TIMEOUT)
        resp.raise_for_status()
        config = resp.json()
        cameras = list(config.get("cameras", {}).keys())
        _CACHED_CAMERAS = sorted(cameras)
        _CACHED_CAMERAS_TIMESTAMP = now
        return _CACHED_CAMERAS
    except Exception as exc:
        logger.error("Error fetching camera list: %s", exc)
        return _CACHED_CAMERAS if _CACHED_CAMERAS is not None else []


async def fetch_recording_clip(
    client: httpx.AsyncClient,
    camera: str,
    start_ts: int,
    end_ts: int,
    max_retries: int = MAX_RETRIES,
) -> bytes | None:
    """Fetch a recording clip for a specific time range."""
    # Frigate API: /api/<camera_name>/start/<start_ts>/end/<end_ts>/clip.mp4
    safe_camera = urllib.parse.quote(camera, safe='')
    return await _fetch_frigate_api(
        client,
        f"{safe_camera}/start/{start_ts}/end/{end_ts}/clip.mp4",
        f"clip.mp4 for {camera} ({start_ts}-{end_ts})",
        "video/mp4",
        max_retries=max_retries,
    )


async def fetch_recent_events(client: httpx.AsyncClient, camera: str, limit: int = 5) -> list[dict]:
    """Fetch the most recent events for a specific camera that have clips."""
    try:
        params = {
            "camera": camera,
            "limit": limit,
            "has_clip": 1,
        }
        resp = await client.get(
            f"{FRIGATE_URL}/api/events",
            params=params,
            auth=_http_auth(),
            timeout=FRIGATE_TIMEOUT,
        )
        resp.raise_for_status()
        events = resp.json()
        if events and isinstance(events, list):
            return events
        return []
    except Exception as exc:
        logger.error("Error fetching recent events for %s: %s", camera, exc)
        return []


async def fetch_video_data_robust(
    client: httpx.AsyncClient, camera: str, event_id: str | None = None, duration: int = 30
) -> bytes | None:
    """
    Robustly fetch video data by trying:
    1. Pre-generated event clip (with retries for new events)
    2. Precise recording clip using event start/end times
    3. Rough recording clip using current time or duration
    """
    data = None

    # 1. Try pre-generated event clip
    if event_id:
        # Retry loop for new events that might still be processing
        for _ in range(5):
            data = await fetch_event_media(client, event_id, "clip", max_retries=1)
            if data:
                return data
            await asyncio.sleep(2)

        # 2. Try precise recording clip using event times
        logger.info("Event clip not found for %s, trying precise recording fallback...", event_id)
        event_details = await fetch_event_details(client, event_id)
        if event_details:
            s = event_details.get("start_time")
            e = event_details.get("end_time")
            if s:
                # Use provided duration as fallback if end_time hasn't been set yet
                start_ts = int(s)
                end_ts = int(e) if e else start_ts + duration
                data = await fetch_recording_clip(client, camera, start_ts, end_ts)
                if data:
                    return data

    # 3. Final fallback: Rough recording clip
    logger.info("Falling back to rough recording clip for %s (%ds)...", camera, duration)
    now = int(time.time())
    data = await fetch_recording_clip(client, camera, now - (duration + 5), now - 5)
    return data


async def trigger_manual_event(
    client: httpx.AsyncClient, camera: str, label: str = "manual", duration: int = 30
) -> str | None:
    """Trigger a manual event in Frigate to force a recording."""
    try:
        # POST /api/events/<camera>/<label>/create
        url = f"{FRIGATE_URL}/api/events/{urllib.parse.quote(camera, safe='')}/{urllib.parse.quote(label, safe='')}/create"
        params = {"include_recording": "1", "duration": str(duration)}
        
        if DEBUG:
            logger.debug("Triggering manual event: %s params=%s", mask_url(url), params)

        resp = await client.post(url, params=params, auth=_http_auth(), timeout=FRIGATE_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("event_id")
    except Exception as exc:
        logger.error("Error triggering manual event for %s: %s", camera, exc)
        return None


# ─────────────────────── Event Filtering ─────────────────────────────


def matches_monitor_config(camera: str, zones: list[str]) -> bool:
    """Check whether a camera/zones pair matches the MONITOR_CONFIG filter.

    If MONITOR_CONFIG is empty, everything passes. Shared by both raw Frigate
    events and review items, since both shapes reduce to (camera, zones).
    """
    if not MONITOR_CONFIG:
        return True

    if camera not in MONITOR_CONFIG:
        return False

    allowed_zones = MONITOR_CONFIG[camera]
    if "all" in allowed_zones:
        return True

    # Match if any zone is in the allowed list
    return not allowed_zones.isdisjoint(zones)


def in_night_window(now_local: dt_time, start: dt_time, end: dt_time) -> bool:
    """True if now_local falls in [start, end). Handles windows that cross midnight
    (e.g. start=22:00, end=06:00) as well as normal same-day windows.

    NIGHT_ALERT_START == NIGHT_ALERT_END is a degenerate empty interval and means
    "never notify" for listed cameras (start <= now_local < end is never true when
    start == end). This is intentional fallout of the interval semantics, not a bug
    to special-case — a misconfigured equal start/end is effectively "always off",
    which is an acceptable (if unhelpful) result for a user config mistake."""
    if start <= end:
        return start <= now_local < end
    return now_local >= start or now_local < end


def matches_night_alert_schedule(camera: str, now: float) -> bool:
    """Check whether *camera* is allowed to notify at epoch *now*.

    Cameras outside NIGHT_ALERT_CAMERAS are unaffected (always True). Cameras in
    the list only pass while local time (TIMEZONE) is inside the configured window.
    """
    if camera not in NIGHT_ALERT_CAMERAS:
        return True
    now_local = datetime.fromtimestamp(now, tz=_CACHED_TZ).time()
    return in_night_window(now_local, NIGHT_ALERT_START, NIGHT_ALERT_END)


# ─────────────────────── Caption Formatting ──────────────────────────


try:
    _CACHED_TZ = ZoneInfo(TIMEZONE)
except ZoneInfoNotFoundError:
    _CACHED_TZ = timezone.utc

def _epoch_to_datetime(epoch: float | None) -> str:
    """Convert epoch timestamp to a human-readable datetime string."""
    if epoch is None or epoch == 0:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(epoch, tz=_CACHED_TZ)
        # Use explicit "UTC" for timezone.utc to maintain exact backwards compatibility in formatting
        if _CACHED_TZ is timezone.utc:
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_sub_label(event: dict) -> tuple[str | None, float | None]:
    """Parse Frigate's sub_label field into (name, score).

    Frigate returns sub_label as either ["name", score], a plain string, or a
    dict, from either the top-level or nested 'data' field.
    """
    raw_sub_label = event.get("sub_label")
    if not raw_sub_label and "data" in event:
        raw_sub_label = event.get("data", {}).get("sub_label")

    if isinstance(raw_sub_label, list) and len(raw_sub_label) >= 1:
        name = str(raw_sub_label[0])
        score = None
        if len(raw_sub_label) >= 2:
            try:
                score = float(raw_sub_label[1])
            except (ValueError, TypeError):
                pass
        return name, score
    if isinstance(raw_sub_label, dict):
        return raw_sub_label.get("label") or raw_sub_label.get("name"), raw_sub_label.get("score")
    if isinstance(raw_sub_label, str) and raw_sub_label:
        return raw_sub_label, None
    return None, None


def format_caption(event: dict) -> str:
    """Build an HTML caption for the Telegram animation message.

    Includes face recognition sub_label when available from Frigate.
    Handles sub_label as [name, score] array, plain string, or dictionary.
    """
    event_id = event.get("id", "unknown")
    camera = event.get("camera", "unknown")
    label = event.get("label", "object")
    sub_label_name, sub_label_score = _parse_sub_label(event)

    zones = ", ".join(event.get("zones", [])) or "N/A"
    score = event.get("top_score")
    score_str = f"{score:.0%}" if score else "N/A"
    start_time = _epoch_to_datetime(event.get("start_time"))
    end_time = _epoch_to_datetime(event.get("end_time"))

    if DEBUG and sub_label_name:
        logger.debug("Event %s parsed sub_label: %s (score=%s)", event_id, sub_label_name, sub_label_score)

    lines = [
        f"🚨 <b>Detection Alert</b>",
        f"",
        f"📷 <b>Camera:</b> {html.escape(camera)}",
        f"🏷️ <b>Label:</b> {html.escape(label)} ({score_str})",
        f"📍 <b>Zone(s):</b> {html.escape(zones)}",
    ]

    # Face recognition: show recognized name when sub_label is present
    if sub_label_name:
        if sub_label_score is not None:
            lines.append(f"👤 <b>Recognized:</b> {html.escape(sub_label_name)} ({sub_label_score:.0%})")
        else:
            lines.append(f"👤 <b>Recognized:</b> {html.escape(sub_label_name)}")

    lines.append(f"📅 <b>Time:</b> {start_time}")

    # Only show end time if event has ended
    if event.get("end_time"):
        lines.append(f"🕑 <b>End:</b> {end_time}")

    lines.append("")

    # External event link (Cloudflare Tunnel URL)
    if EXTERNAL_URL:
        event_url = f"{EXTERNAL_URL}/events/{event_id}"
        lines.append(f'🔗 <a href="{html.escape(event_url, quote=True)}">View Event in Frigate</a>')

    return "\n".join(lines)



def format_grouped_caption(data: dict) -> str:
    """Build an HTML caption for a (possibly merged) notification group.

    Unlike format_caption (single raw Frigate event, still used by manual
    commands), this accepts the aggregated payload built by
    send_grouped_notification: unioned labels/zones/recognized names across
    every constituent event, spanning the group's earliest start to latest
    end.
    """
    camera = data.get("camera", "unknown")
    labels = sorted(data.get("labels") or ["object"])
    zones = sorted(data.get("zones") or [])
    score = data.get("top_score")
    score_str = f"{score:.0%}" if score else "N/A"
    start_time = _epoch_to_datetime(data.get("start_time"))
    end_time = _epoch_to_datetime(data.get("end_time"))

    lines = [
        f"🚨 <b>Detection Alert</b>",
        f"",
        f"📷 <b>Camera:</b> {html.escape(camera)}",
        f"🏷️ <b>Label:</b> {html.escape(', '.join(labels))} ({score_str})",
        f"📍 <b>Zone(s):</b> {html.escape(', '.join(zones)) if zones else 'N/A'}",
    ]

    for name, sub_score in data.get("sub_labels") or []:
        if sub_score is not None:
            lines.append(f"👤 <b>Recognized:</b> {html.escape(name)} ({sub_score:.0%})")
        else:
            lines.append(f"👤 <b>Recognized:</b> {html.escape(name)}")

    lines.append(f"📅 <b>Time:</b> {start_time}")

    # Only show end time if the group has actually finished
    if data.get("end_time"):
        lines.append(f"🕑 <b>End:</b> {end_time}")

    lines.append("")

    # External event link (Cloudflare Tunnel URL) — points at the first
    # constituent event, since a merged group has no single event page.
    if EXTERNAL_URL:
        event_id = data.get("primary_event_id", "")
        event_url = f"{EXTERNAL_URL}/events/{event_id}"
        lines.append(f'🔗 <a href="{html.escape(event_url, quote=True)}">View Event in Frigate</a>')

    return "\n".join(lines)


# ─────────────────────── Telegram Notification ───────────────────────


async def _send_fallback_photo(bot: Bot, photo_data: bytes, caption: str) -> None:
    """Shared send_photo call shape for send_grouped_notification's two
    photo-fallback branches (clip missing/too-large, and send_video raised).
    A failure here propagates to the caller — it is not swallowed here."""
    await bot.send_photo(
        chat_id=TELEGRAM_CHAT_ID,
        photo=photo_data,
        caption=caption,
        parse_mode=ParseMode.HTML,
        filename="snapshot.jpg",
        **TELEGRAM_TIMEOUT_KWARGS,
    )


async def _send_fallback_gif(bot: Bot, gif_data: bytes, caption: str) -> None:
    """Shared send_animation call shape for send_grouped_notification's two
    gif-fallback branches (clip missing/too-large, and send_video raised).
    A failure here propagates to the caller — it is not swallowed here."""
    await bot.send_animation(
        chat_id=TELEGRAM_CHAT_ID,
        animation=gif_data,
        caption=caption,
        parse_mode=ParseMode.HTML,
        filename="preview.gif",
        **TELEGRAM_TIMEOUT_KWARGS,
    )


def _split_windows(start: int, end: int, n: int) -> list[tuple[int, int]]:
    """Split [start, end] into n contiguous integer windows (callers ensure 1 <= n <= end - start)."""
    d = end - start
    return [(start + i * d // n, start + (i + 1) * d // n) for i in range(n)]


def _planned_part_count(size_bytes: int, duration_s: int) -> int:
    """Parts needed so each stays under MAX_TELEGRAM_FILE_SIZE, at >= 1 s per part.

    Reads MAX_TELEGRAM_FILE_SIZE at call time (not as a default arg) so it
    follows the module global.
    """
    n = max(2, math.ceil(size_bytes / (MAX_TELEGRAM_FILE_SIZE * CLIP_SPLIT_SAFETY)))
    return min(n, duration_s)


async def send_grouped_notification(bot: Bot, group: PendingGroup, http_client: httpx.AsyncClient) -> None:
    """Send a **single** consolidated Telegram message for a notification group.

    A group may span one or several Frigate event IDs that were fragments of
    the same physical activity (see grouping.py). Flow:
    1. Fetch each constituent event's own details (real start/end/label/
       zones/sub_label — the review item's own bounds aren't always tight).
    2. Aggregate: union labels/zones/recognized-names, earliest start →
       latest end across all constituent events (caption only).
    3. Build a clip PER EVENT, over that event's own start/end window —
       never one clip spanning the merged group's union window. Frigate's
       recording retention here only persists motion-flagged segments with
       a small pre/post capture buffer, not continuous footage, so a union
       window spanning a multi-event gap (or the whole merged duration)
       comes back empty even though each event's own tight window is
       reliably stored. Falls back to the pre-generated per-event clip if
       the recording endpoint has nothing for that event's window.
    4. Photo fallback is event-anchored: event snapshot.jpg → event
       thumbnail.jpg → live camera frame (last resort — by send time the
       subject may already have left the live frame).
    5. Send ONE message: video(s) → GIF (event preview.gif, only fetched
       once no clip is available at all) → photo → text-only. Multiple
       successful per-event clips are sent as separate videos tagged
       "(Event N/M)"; an individual event's recording clip that still
       exceeds Telegram's 50MB limit is split into N time-slices of that
       event's own window (at most MAX_CLIP_PARTS are fetched and sent).
    """
    details_list = await asyncio.gather(
        *[fetch_event_details(http_client, event_id) for event_id in group.event_ids]
    )
    details_list = [d for d in details_list if d]
    details_list.sort(key=lambda d: d.get("start_time") or 0)

    now = time.time()
    starts = [d["start_time"] for d in details_list if d.get("start_time")]
    ends = [d.get("end_time") or now for d in details_list]
    union_start = min(starts) if starts else group.first_start
    union_end = max(ends) if ends else group.last_activity_end

    labels = {d["label"] for d in details_list if d.get("label")} or set(group.labels)
    zones: set[str] = set()
    for d in details_list:
        zones.update(d.get("zones", []))

    sub_labels: dict[str, float | None] = {}
    for d in details_list:
        name, score = _parse_sub_label(d)
        if name and (name not in sub_labels or (score or 0) > (sub_labels[name] or 0)):
            sub_labels[name] = score

    scores = [d["top_score"] for d in details_list if d.get("top_score")]
    top_score = max(scores) if scores else None
    primary_event_id = details_list[0]["id"] if details_list else next(iter(group.event_ids), "unknown")

    caption = format_grouped_caption({
        "camera": group.camera,
        "labels": labels,
        "zones": zones,
        "sub_labels": list(sub_labels.items()),
        "top_score": top_score,
        "start_time": union_start,
        "end_time": union_end,
        "primary_event_id": primary_event_id,
    })

    # One clip fetch per constituent event, over that event's own tight
    # start/end window (see docstring point 3 for why). Events with no
    # resolved details (fetch_event_details failed) have no window to try
    # the recording endpoint with — go straight to the pre-generated clip.
    events_for_clips = [(d["id"], d.get("start_time"), d.get("end_time")) for d in details_list] or [
        (event_id, None, None) for event_id in group.event_ids
    ]

    async def _fetch_event_clip(
        event_id: str, start: float | None, end: float | None
    ) -> tuple[bytes | None, tuple[int, int] | None]:
        """Return (clip, padded window) — window is None unless the clip came
        from the recording endpoint, the only source that can be re-sliced by
        time. An event clip.mp4 fallback means recordings for that window are
        evidently unavailable, so splitting it via the recording endpoint is moot."""
        if start is None:
            return await fetch_event_media(http_client, event_id, "clip"), None
        pad_start = max(0, int(start) - CLIP_PADDING_SECONDS)
        pad_end = int(end or start) + CLIP_PADDING_SECONDS
        data = await fetch_recording_clip(http_client, group.camera, pad_start, pad_end)
        if data:
            return data, (pad_start, pad_end)
        return await fetch_event_media(http_client, event_id, "clip"), None

    # Snapshot fetched concurrently with the clips — it's only used as a
    # video thumbnail or as the fallback photo, never needs to wait on them.
    # Photo priority is event-anchored, not live: fetch_camera_snapshot()
    # hits the camera's *current* live frame, which by send time is 45s+
    # after the event ended (grouping delay) — the subject is long gone.
    # The event's own snapshot.jpg (Frigate's best captured frame of the
    # event) is tried first, then the event thumbnail, and only then the
    # live camera frame as a last resort.
    *event_clips, photo_data = await asyncio.gather(
        *[_fetch_event_clip(eid, start, end) for eid, start, end in events_for_clips],
        fetch_event_media(http_client, primary_event_id, "snapshot"),
    )
    if not photo_data:
        photo_data = await fetch_event_media(http_client, primary_event_id, "thumbnail")
    if not photo_data:
        photo_data = await fetch_camera_snapshot(http_client, group.camera)

    # Resolve each successful event to the video part(s) it actually
    # contributes — an event whose clip is oversized AND unsplittable (not
    # from the recording endpoint) or whose parts all fail contributes none.
    # The "(Event N/M)" tag is built from events that actually contribute
    # below, not from `successful`, so it never promises a video that never
    # arrives.
    successful = [
        (eid, data, window)
        for (eid, _s, _e), (data, window) in zip(events_for_clips, event_clips)
        if data
    ]
    # Drop the other reference so an oversized full clip (can be hundreds of
    # MB) is freed before its parts are fetched.
    event_clips = None
    event_parts: list[tuple[str, list[tuple[bytes, str]]]] = []
    for i in range(len(successful)):
        eid, data, window = successful[i]
        successful[i] = None
        if len(data) <= MAX_TELEGRAM_FILE_SIZE:
            event_parts.append((eid, [(data, "")]))
            continue
        size = len(data)
        data = None  # release the full clip; only its size is needed now
        if window is None:
            logger.warning(
                "Clip for event %s on %s (%d bytes) exceeds size limit and has no recording window to split",
                eid, group.camera, size,
            )
            event_parts.append((eid, []))
            continue
        pad_start, pad_end = window
        duration = pad_end - pad_start
        if duration < 2:
            logger.warning("Clip for event %s on %s exceeds size limit; window too short to split", eid, group.camera)
            event_parts.append((eid, []))
            continue
        n = _planned_part_count(size, duration)
        to_fetch = _split_windows(pad_start, pad_end, n)[:MAX_CLIP_PARTS]
        logger.info(
            "Clip for event %s on %s (%d bytes) exceeds %d MB limit; splitting into %d parts",
            eid, group.camera, size, MAX_TELEGRAM_FILE_SIZE // (1024 * 1024), n,
        )
        if n > MAX_CLIP_PARTS:
            logger.info("Event %s on %s: sending only the first %d of %d parts", eid, group.camera, MAX_CLIP_PARTS, n)
        # Fetched sequentially, not gathered: each Frigate clip request spawns
        # its own ffmpeg writing into /tmp/cache. Single attempt per part (the
        # recording evidently exists), so the polling tick stalls ~MAX_CLIP_PARTS x
        # (FRIGATE_TIMEOUT + UPLOAD_TIMEOUT) per event when requests time out, more
        # if they trickle (both timeouts are per-operation, not total deadlines).
        # ponytail: all kept parts are buffered before sending (~full clip size,
        # capped at MAX_CLIP_PARTS x 50 MB per event); stream part-by-part if RAM bites.
        parts: list[tuple[bytes, str]] = []
        for part_idx, (w_start, w_end) in enumerate(to_fetch, start=1):
            part = await fetch_recording_clip(http_client, group.camera, w_start, w_end, max_retries=1)
            if not part or len(part) > MAX_TELEGRAM_FILE_SIZE:
                logger.warning(
                    "Event %s on %s: part %d/%d (%d bytes) missing or still over the size limit; skipping",
                    eid, group.camera, part_idx, n, len(part or b""),
                )
                continue
            parts.append((part, f"\n\n📹 <i>(Part {part_idx}/{n})</i>"))
        if parts and n > MAX_CLIP_PARTS:
            last_bytes, last_suffix = parts[-1]
            parts[-1] = (
                last_bytes,
                f"{last_suffix} <i>(clip truncated: only the first {MAX_CLIP_PARTS} of {n} parts were fetched)</i>",
            )
        event_parts.append((eid, parts))

    contributing = [(eid, parts) for eid, parts in event_parts if parts]
    multi_event = len(contributing) > 1
    clips_to_send: list[tuple[bytes, str]] = []
    for idx, (eid, parts) in enumerate(contributing, start=1):
        event_tag = f" <i>(Event {idx}/{len(contributing)})</i>" if multi_event else ""
        for video_bytes, part_suffix in parts:
            clips_to_send.append((video_bytes, f"{caption}{event_tag}{part_suffix}"))

    # GIF is the tier between video and photo, but only worth fetching once
    # no clip is going out at all — the common (clip succeeds) case never
    # pays for this extra request.
    gif_data: bytes | None = None
    if not clips_to_send:
        gif_data = await fetch_event_media(http_client, primary_event_id, "gif")

    if clips_to_send:
        # Narrowly scoped to the send_video call(s) only — a failure here is
        # the one case with a real fallback story (photo/text instead of
        # video). photo_data/text failures below propagate normally instead
        # of being caught here, so they're never mistaken for a video
        # failure and never double-sent (see _send_fallback_photo callers).
        sent_count = 0
        try:
            for i, (video_bytes, cap) in enumerate(clips_to_send):
                await bot.send_video(
                    chat_id=TELEGRAM_CHAT_ID,
                    video=video_bytes,
                    thumbnail=photo_data if i == 0 else None,
                    caption=cap,
                    parse_mode=ParseMode.HTML,
                    filename=f"clip_part{i+1}.mp4" if len(clips_to_send) > 1 else "clip.mp4",
                    supports_streaming=True,
                    **TELEGRAM_TIMEOUT_KWARGS,
                )
                sent_count += 1
            logger.info(
                "Group on %s (%d event(s)) → sent %d video clip(s) with caption ✓",
                group.camera, len(group.event_ids), len(clips_to_send),
            )
        except Exception as exc:
            logger.error("Failed to send Telegram video notification for group on %s: %s", group.camera, exc)
            if sent_count > 0:
                # A multi-event group can fail partway through — one or
                # more videos already reached the user, so a full GIF/
                # photo/text fallback now would be a confusing duplicate,
                # not a recovery. Just note the partial delivery.
                logger.warning(
                    "Group on %s → %d/%d video clip(s) sent before failure; not sending a duplicate fallback",
                    group.camera, sent_count, len(clips_to_send),
                )
            else:
                try:
                    if not gif_data:
                        gif_data = await fetch_event_media(http_client, primary_event_id, "gif")
                    if gif_data:
                        await _send_fallback_gif(
                            bot, gif_data, f"{caption}\n\n⚠️ <i>(Video upload failed, sent GIF)</i>"
                        )
                        logger.info("Group on %s → fallback GIF sent successfully ✓", group.camera)
                    elif photo_data:
                        await _send_fallback_photo(
                            bot, photo_data, f"{caption}\n\n⚠️ <i>(Video upload failed, sent snapshot)</i>"
                        )
                        logger.info("Group on %s → fallback photo sent successfully ✓", group.camera)
                    else:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=f"{caption}\n\n⚠️ <i>(Video upload failed)</i>",
                            parse_mode=ParseMode.HTML,
                            **TELEGRAM_TIMEOUT_KWARGS,
                        )
                except Exception as fallback_exc:
                    logger.error("Fallback notification also failed for group on %s: %s", group.camera, fallback_exc)

    elif gif_data:
        await _send_fallback_gif(bot, gif_data, caption)
        logger.info("Group on %s → sent GIF with caption (clip unavailable or exceeds size limit)", group.camera)

    elif photo_data:
        await _send_fallback_photo(bot, photo_data, caption)
        logger.info("Group on %s → sent photo with caption (clip and GIF unavailable or exceed size limit)", group.camera)

    else:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=caption,
            parse_mode=ParseMode.HTML,
            **TELEGRAM_TIMEOUT_KWARGS,
        )
        logger.info("Group on %s → sent text only (no media available)", group.camera)


# ─────────────────── Telegram Command Handlers ───────────────────────


def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            logger.warning(
                "Unauthorized command attempt from chat_id=%s",
                update.effective_chat.id if update.effective_chat else "unknown",
            )
            return
        return await func(update, context)

    return wrapper


@authorized_only
async def cmd_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await state.enable()
    await update.effective_chat.send_message("✅ Notifications enabled.")
    logger.info("Notifications enabled via Telegram command.")


@authorized_only
async def cmd_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await state.disable()
    await update.effective_chat.send_message("🔕 Notifications disabled.")
    logger.info("Notifications disabled via Telegram command.")

@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [
        "<b>Frigate-Telegram Bot Help</b>",
        "",
        "📱 <b>Menu Hub</b>",
        "/menu - Open the main interaction menu",
        "",
        "🔔 <b>Notifications</b>",
        "/enable - Turn on event alerts",
        "/disable - Turn off event alerts",
        "",
        "🎥 <b>Live View & Media</b>",
        "/cameras - List all registered cameras",
        "/photo [camera] - Get snapshot",
        "/photo_all - Get snapshots from all cameras",
        "/video [camera] - Record 30s manual clip (requires server-side continuous recording)",
        "/video_all - Record 30s clips from all cameras (requires server-side continuous recording)",
        "/video_last [camera] - Get last event clip",
        "/video_all_last - Get last event clips for all cameras",
        "",
        "📊 <b>Information & Tools</b>",
        "/status - Show bot configuration and health",
        "/help - Show this help message",
    ]
    await update.effective_chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "👋 <b>Welcome to Frigate-Telegram!</b>\n\n"
        "I'll send you rich notifications for Frigate detection events.\n\n"
        "Use the menu below or /help to see available commands."
    )
    await update.effective_chat.send_message(welcome, reply_markup=await get_main_menu(), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main interaction menu."""
    await update.effective_chat.send_message("📱 <b>Main Menu</b>", reply_markup=await get_main_menu(), parse_mode=ParseMode.HTML)

@authorized_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_emoji = "🔔" if state.enabled else "🔕"
    status_text = "Enabled" if state.enabled else "Disabled"
    cameras = ", ".join(MONITOR_CONFIG.keys()) if MONITOR_CONFIG else "All Cameras"
    health_cams = ", ".join(HEALTH_MONITOR_CAMERAS) if HEALTH_MONITOR_CAMERAS else "All Cameras"
    if NIGHT_ALERT_CAMERAS:
        night_cams = f"{', '.join(sorted(NIGHT_ALERT_CAMERAS))} ({NIGHT_ALERT_START.strftime('%H:%M')}–{NIGHT_ALERT_END.strftime('%H:%M')})"
    else:
        night_cams = "None configured"

    health_lines = []
    if camera_health_monitor.states:
        for cam, cstate in sorted(camera_health_monitor.states.items()):
            if cstate.is_offline:
                downtime = format_downtime(time.time() - (cstate.first_failure_ts or time.time()))
                health_lines.append(f"• <code>{html.escape(cam)}</code>: 🔴 Offline ({cstate.current_fps:.1f} fps) - Down for {downtime}")
            else:
                health_lines.append(f"• <code>{html.escape(cam)}</code>: 🟢 Online ({cstate.current_fps:.1f} fps)")
    cache_pct = cache_health_monitor.last_pct
    if cache_pct is not None:
        cache_emoji = "🔴" if cache_pct >= HEALTH_CACHE_THRESHOLD_PCT else "🟢"
        health_lines.append(f"• 💾 <b>Frigate cache:</b> {cache_emoji} {cache_pct:.0f}%")

    lines = [
        "📊 <b>Bot Status</b>",
        "",
        f"<b>Notifications:</b> {status_emoji} {status_text}",
        f"<b>Polling Interval:</b> ⏱ {POLLING_INTERVAL}s",
        f"<b>Monitored Cameras:</b> 🎥 {html.escape(cameras)}",
        f"<b>Health Monitored:</b> 🩺 {html.escape(health_cams)}",
        f"<b>Night Alert Cameras:</b> 🌙 {html.escape(night_cams)}",
    ]
    if health_lines:
        lines.append("")
        lines.append("📹 <b>Camera Health:</b>")
        lines.extend(health_lines)
    lines.extend([
        "",
        "🛠 <b>Configuration</b>",
        f"<b>Frigate URL:</b> 🔗 {html.escape(mask_url(FRIGATE_URL))}",
        f"<b>External URL:</b> 🌐 {html.escape(mask_url(EXTERNAL_URL)) if EXTERNAL_URL else 'Not configured'}",
        f"<b>Frigate Timeout:</b> ⏳ {FRIGATE_TIMEOUT}s",
        f"<b>Upload Timeout:</b> 📤 {UPLOAD_TIMEOUT}s",
    ])
    await update.effective_chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_cameras(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]
    cameras = await fetch_camera_list(http_client)
    if not cameras:
        await update.effective_chat.send_message("Could not retrieve camera list from Frigate.")
        return

    lines = ["<b>Registered Cameras:</b>", ""]
    for cam in cameras:
        cstate = camera_health_monitor.states.get(cam)
        indicator = "🔴 Offline" if (cstate and cstate.is_offline) else "🟢 Online"
        lines.append(f"• <code>{html.escape(cam)}</code>: {indicator}")

    await update.effective_chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)



async def get_main_menu() -> InlineKeyboardMarkup:
    """Create the top-level main menu."""
    keyboard = [
        [
            InlineKeyboardButton("📸 Snapshots", callback_data="nav:snapshot"),
            InlineKeyboardButton("🎬 Clips", callback_data="nav:video"),
        ],
        [
            InlineKeyboardButton("⏮️ Recent", callback_data="nav:video_last"),
            InlineKeyboardButton("📊 Status", callback_data="cmd:status:none"),
        ],
        [
            InlineKeyboardButton(
                "🔔 Notifications: ON" if state.enabled else "🔕 Notifications: OFF",
                callback_data="toggle:notifications"
            ),
            InlineKeyboardButton("❓ Help", callback_data="nav:help"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def get_camera_selection_menu(
    http_client: httpx.AsyncClient, command: str, include_all: bool = False
) -> InlineKeyboardMarkup | None:
    """Create an inline keyboard with buttons for each camera."""
    cameras = await fetch_camera_list(http_client)
    if not cameras:
        return None

    keyboard = []
    # Create rows of 2 buttons
    for i in range(0, len(cameras), 2):
        row = []
        cam1 = cameras[i]
        row.append(InlineKeyboardButton(cam1, callback_data=f"cmd:{command}:{cam1}"))
        if i + 1 < len(cameras):
            cam2 = cameras[i + 1]
            row.append(InlineKeyboardButton(cam2, callback_data=f"cmd:{command}:{cam2}"))
        keyboard.append(row)
    
    if include_all:
        all_cmd = f"photo_all" if command == "photo" else f"video_all" if command == "video" else "video_all_last"
        keyboard.append([InlineKeyboardButton("✨ All Cameras", callback_data=f"all:{all_cmd}")])

    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="nav:main")])
    return InlineKeyboardMarkup(keyboard)


@authorized_only
async def cmd_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]

    if not context.args:
        menu = await get_camera_selection_menu(http_client, "photo")
        if menu:
            await update.effective_chat.send_message("📸 Select a camera:", reply_markup=menu)
        else:
            await update.effective_chat.send_message("Could not retrieve camera list from Frigate.")
        return

    camera_name = " ".join(context.args)
    
    await update.effective_chat.send_action(ChatAction.UPLOAD_PHOTO)
    photo_data = await fetch_camera_snapshot(http_client, camera_name)
    if not photo_data:
        await update.effective_chat.send_message(f"Could not fetch snapshot for camera: {camera_name}")
        return

    await update.effective_chat.send_photo(
        photo=photo_data,
        caption=f"📷 Snapshot: {html.escape(camera_name)}",
        parse_mode=ParseMode.HTML,
        filename=f"{camera_name}.jpg",
        **TELEGRAM_TIMEOUT_KWARGS,
    )


@authorized_only
async def cmd_photo_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]
    cameras = await fetch_camera_list(http_client)
    if not cameras:
        await update.effective_chat.send_message("Could not retrieve camera list from Frigate.")
        return

    # Fetch and send snapshots
    async def fetch_and_send(camera):
        data = await fetch_camera_snapshot(http_client, camera)
        if data:
            await update.effective_chat.send_photo(
                photo=data,
                caption=f"📷 Snapshot: {html.escape(camera)}",
                parse_mode=ParseMode.HTML,
                filename=f"{camera}.jpg",
                **TELEGRAM_TIMEOUT_KWARGS,
            )
        else:
            await update.effective_chat.send_message(f"❌ Failed to fetch snapshot for <code>{html.escape(camera)}</code>", parse_mode=ParseMode.HTML)

    await asyncio.gather(*[fetch_and_send(cam) for cam in cameras])


@authorized_only
async def cmd_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]

    if not context.args:
        menu = await get_camera_selection_menu(http_client, "video")
        if menu:
            await update.effective_chat.send_message("🎥 Select a camera to record:", reply_markup=menu)
        else:
            await update.effective_chat.send_message("Could not retrieve camera list from Frigate.")
        return

    camera_name = " ".join(context.args)

    duration = 30
    await update.effective_chat.send_message(f"🎬 Starting {duration}s manual recording for <code>{html.escape(camera_name)}</code>...", parse_mode=ParseMode.HTML)
    await update.effective_chat.send_action(ChatAction.RECORD_VIDEO)

    try:
        # Trigger manual event to force recording
        event_id = await trigger_manual_event(http_client, camera_name, label="telegram_request", duration=duration)
        
        if not event_id:
            await update.effective_chat.send_message(f"❌ Failed to start recording for {html.escape(camera_name)}", parse_mode=ParseMode.HTML)
            return

        # Wait for recording to complete + buffer (Frigate needs time to finalize segments)
        await asyncio.sleep(duration + 10)

        # Robust fetch
        video_data = await fetch_video_data_robust(http_client, camera_name, event_id, duration)
        
        if not video_data:
            await update.effective_chat.send_message(f"❌ Could not fetch video clip for {html.escape(camera_name)}", parse_mode=ParseMode.HTML)
            return

        await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)
        await update.effective_chat.send_video(
            video=video_data,
            caption=f"🎬 Clip: {html.escape(camera_name)}",
            parse_mode=ParseMode.HTML,
            filename=f"{camera_name}_{event_id if event_id else 'manual'}.mp4",
            supports_streaming=True,
            **TELEGRAM_TIMEOUT_KWARGS,
        )
    except Exception as e:
        logger.error(f"Error in cmd_video for {camera_name}: {e}", exc_info=DEBUG)
        await update.effective_chat.send_message(
            f"⚠️ An error occurred while recording the video for {html.escape(camera_name)}.",
            parse_mode=ParseMode.HTML,
        )


@authorized_only
async def cmd_video_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]
    cameras = await fetch_camera_list(http_client)
    if not cameras:
        await update.effective_chat.send_message("Could not retrieve camera list from Frigate.")
        return

    await update.effective_chat.send_message(f"🎬 Fetching 30s clips for {len(cameras)} cameras...", parse_mode=ParseMode.HTML)

    # Fetch and send video clips
    async def fetch_and_send(camera):
        duration = 30
        try:
            # Trigger manual event
            event_id = await trigger_manual_event(http_client, camera, label="telegram_request", duration=duration)
            if not event_id:
                await update.effective_chat.send_message(f"❌ Failed to start recording for <code>{html.escape(camera)}</code>", parse_mode=ParseMode.HTML)
                return

            # Wait for recording to complete + buffer (Frigate needs time to finalize segments)
            await asyncio.sleep(duration + 10)

            # Robust fetch
            data = await fetch_video_data_robust(http_client, camera, event_id, duration)

            if data:
                await update.effective_chat.send_video(
                    video=data,
                    caption=f"🎬 Clip: {html.escape(camera)}",
                    parse_mode=ParseMode.HTML,
                    filename=f"{camera}_{event_id}.mp4",
                    supports_streaming=True,
                    **TELEGRAM_TIMEOUT_KWARGS,
                )
            else:
                await update.effective_chat.send_message(f"❌ Failed to fetch video clip for <code>{html.escape(camera)}</code> (Event {html.escape(event_id)})", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error in fetch_and_send for {camera}: {e}", exc_info=DEBUG)

    # Note: This will take (duration + 5) seconds total as all tasks sleep in parallel
    await asyncio.gather(*[fetch_and_send(cam) for cam in cameras])


@authorized_only
async def cmd_video_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_chat.send_message("Usage: /video_last <camera_name>")
        return

    camera_name = " ".join(context.args)
    http_client = context.bot_data["http_client"]

    await update.effective_chat.send_message(f"🎬 Fetching recent event for <code>{html.escape(camera_name)}</code>...", parse_mode=ParseMode.HTML)

    events = await fetch_recent_events(http_client, camera_name, limit=5)
    if not events:
        await update.effective_chat.send_message(f"❌ No recent events with clips found for {html.escape(camera_name)}", parse_mode=ParseMode.HTML)
        return

    video_data = None
    successful_event = None

    for event in events:
        event_id = event.get("id")
        logger.info("Trying to fetch video for event %s (camera: %s)", event_id, camera_name)
        video_data = await fetch_video_data_robust(http_client, camera_name, event_id)
        if video_data:
            successful_event = event
            break
        logger.warning("Could not fetch video for event %s, trying next...", event_id)

    if not video_data or not successful_event:
        await update.effective_chat.send_message(f"❌ Could not fetch video for any of the last {len(events)} events on {html.escape(camera_name)}", parse_mode=ParseMode.HTML)
        return

    caption = format_caption(successful_event)
    await update.effective_chat.send_video(
        video=video_data,
        caption=caption,
        parse_mode=ParseMode.HTML,
        filename=f"{camera_name}_last.mp4",
        supports_streaming=True,
        **TELEGRAM_TIMEOUT_KWARGS,
    )


@authorized_only
async def cmd_video_all_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    http_client = context.bot_data["http_client"]
    cameras = await fetch_camera_list(http_client)
    if not cameras:
        await update.effective_chat.send_message("Could not retrieve camera list from Frigate.")
        return

    await update.effective_chat.send_message(f"🎬 Fetching last event clips for {len(cameras)} cameras...", parse_mode=ParseMode.HTML)

    async def fetch_and_send(camera):
        events = await fetch_recent_events(http_client, camera, limit=5)
        if not events:
            await update.effective_chat.send_message(f"❌ No recent events with clips for <code>{html.escape(camera)}</code>", parse_mode=ParseMode.HTML)
            return

        video_data = None
        successful_event = None

        for event in events:
            event_id = event.get("id")
            video_data = await fetch_video_data_robust(http_client, camera, event_id)
            if video_data:
                successful_event = event
                break

        if video_data and successful_event:
            caption = format_caption(successful_event)
            await update.effective_chat.send_video(
                video=video_data,
                caption=caption,
                parse_mode=ParseMode.HTML,
                filename=f"{camera}_last.mp4",
                supports_streaming=True,
                **TELEGRAM_TIMEOUT_KWARGS,
            )
        else:
            await update.effective_chat.send_message(f"❌ Failed to fetch video for any of the last {len(events)} events on <code>{html.escape(camera)}</code>", parse_mode=ParseMode.HTML)

    await asyncio.gather(*[fetch_and_send(cam) for cam in cameras])


@authorized_only
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries from inline keyboards."""
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data
    if not data:
        return

    http_client = context.bot_data["http_client"]

    # 1. Navigation handling
    if data.startswith("nav:"):
        _, target = data.split(":", 1)
        if target == "main":
            await query.edit_message_text("📱 <b>Main Menu</b>", reply_markup=await get_main_menu(), parse_mode=ParseMode.HTML)
        elif target == "snapshot":
            menu = await get_camera_selection_menu(http_client, "photo", include_all=True)
            await query.edit_message_text("📸 <b>Snapshots</b>\nSelect a camera or view all:", reply_markup=menu, parse_mode=ParseMode.HTML)
        elif target == "video":
            menu = await get_camera_selection_menu(http_client, "video", include_all=True)
            await query.edit_message_text("🎬 <b>Manual Recordings</b>\nSelect a camera to start 30s recording:", reply_markup=menu, parse_mode=ParseMode.HTML)
        elif target == "video_last":
            menu = await get_camera_selection_menu(http_client, "video_last", include_all=True)
            await query.edit_message_text("⏮️ <b>Latest Activity</b>\nSelect a camera to see the last recorded event:", reply_markup=menu, parse_mode=ParseMode.HTML)
        elif target == "help":
            await cmd_help(update, context)
        return

    # 2. Toggle handling
    if data == "toggle:notifications":
        if state.enabled:
            await state.disable()
        else:
            await state.enable()
        # Refresh the menu
        await query.edit_message_reply_markup(reply_markup=await get_main_menu())
        return

    # 3. "All" command handling
    if data.startswith("all:"):
        _, cmd = data.split(":", 1)
        await query.delete_message()
        if cmd == "photo_all":
            await cmd_photo_all(update, context)
        elif cmd == "video_all":
            await cmd_video_all(update, context)
        elif cmd == "video_all_last":
            await cmd_video_all_last(update, context)
        return

    # 4. Single command handling
    if data.startswith("cmd:"):
        try:
            _, command, camera_name = data.split(":", 2)
        except ValueError:
            logger.warning("Invalid callback data: %s", data)
            return

        # Delete the menu message
        await query.delete_message()

        # Reuse existing command logic by faking args
        context.args = [] if camera_name == "none" else [camera_name]
        
        if command == "photo":
            await cmd_photo(update, context)
        elif command == "video":
            await cmd_video(update, context)
        elif command == "video_last":
            await cmd_video_last(update, context)
        elif command == "status":
            await cmd_status(update, context)
        else:
            logger.warning("Unknown command in callback: %s", command)


# ─────────────────────── Main Polling Loop ───────────────────────────


async def check_camera_health_and_alert(
    bot: Bot,
    client: httpx.AsyncClient,
    now: float | None = None,
) -> None:
    """Evaluate Frigate camera stats and dispatch Telegram alerts on failures or recoveries.

    Guarded end-to-end: this function must always return normally, no matter
    what fails inside it (bad Frigate payload, network error, alert-send
    failure). _polling_tick relies on that — a health-check exception must
    never abort a tick's notification processing.
    """
    if now is None:
        now = time.time()

    try:
        # Fetching stats is expected to fail routinely during ordinary Frigate
        # downtime/restarts — logged at `debug` (matches the rest of this
        # file's convention for transient connectivity, e.g. the polling
        # loop's back-off retry log). The outer `except` below is reserved for
        # genuine bugs (bad payload shape, alert-dispatch errors) and stays at
        # `error`.
        try:
            resp = await client.get(f"{FRIGATE_URL}/api/stats", auth=_http_auth(), timeout=FRIGATE_TIMEOUT)
            if resp.status_code != 200:
                logger.debug("Frigate /api/stats returned HTTP %s", resp.status_code)
                return
            stats_data = resp.json()
        except Exception as exc:
            logger.debug("Failed to fetch Frigate stats for health check: %s", exc)
            return

        # Cache check runs first in its own guard so a failure in either the
        # cache or the camera evaluation never suppresses the other's alert.
        try:
            cache_alert = cache_health_monitor.evaluate(stats_data, HEALTH_CACHE_THRESHOLD_PCT, now=now)
            if cache_alert is not None:
                await _send_health_alert(bot, cache_alert)
        except Exception as exc:
            logger.error("Frigate cache health check failed: %s", exc)

        alerts = camera_health_monitor.evaluate_stats(stats_data, now=now)
        for alert in alerts:
            if alert.alert_type == "offline":
                try:
                    detail = await fetch_log_error_detail(client, FRIGATE_URL, alert.camera, auth=_http_auth())
                    if detail:
                        cstate = camera_health_monitor.states.get(alert.camera)
                        if cstate:
                            cstate.last_error_detail = detail
                            alert.message = camera_health_monitor.format_offline_alert(
                                camera=alert.camera,
                                current_fps=cstate.current_fps,
                                expected_fps=cstate.expected_fps,
                                alert_count=alert.alert_count,
                                error_detail=detail,
                            )
                except Exception as exc:
                    logger.debug("Failed to fetch log error detail for %s: %s", alert.camera, exc)

            await _send_health_alert(bot, alert)
    except Exception as exc:
        logger.error("Camera health check failed: %s", exc)


async def _send_health_alert(bot: Bot, alert: HealthAlert) -> None:
    """Send a health alert; a send failure is logged, never raised."""
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=alert.message,
            parse_mode=ParseMode.HTML,
            **TELEGRAM_TIMEOUT_KWARGS,
        )
    except Exception as exc:
        logger.error("Failed to send health alert for %s: %s", alert.camera, exc)


async def _polling_tick(
    bot: Bot,
    http_client: httpx.AsyncClient,
    pending: dict[str, PendingGroup],
    last_poll_ts: float,
    now: float | None = None,
    notifications_enabled: bool = True,
) -> float:
    """Run one polling iteration: fetch new review items, merge them into
    pending groups, finalize+send any that have gone quiet, and return the
    new last_poll_ts. Extracted from polling_loop so the hold→merge→send
    flow can be unit tested across multiple ticks without an infinite loop.

    The camera-health check runs in a `finally` block so it always fires
    exactly once per tick — regardless of whether notification processing
    below raises, and regardless of *notifications_enabled* — decoupling
    health alerts from both crashes in the notification path and from
    /disable. When notifications are disabled, *last_poll_ts* is left
    unchanged (not advanced), so re-enabling still picks up everything
    missed since the last successful poll.
    """
    if now is None:
        now = time.time()

    new_last_poll_ts = last_poll_ts
    try:
        if notifications_enabled:
            reviews = await fetch_review_items(http_client, last_poll_ts)
            new_last_poll_ts = time.time()

            matched = [
                r for r in reviews
                if matches_monitor_config(r.get("camera", ""), r.get("data", {}).get("zones", []))
                and matches_night_alert_schedule(r.get("camera", ""), now)
            ]
            for review in matched:
                merge_into_pending(pending, review, now, EVENT_MERGE_GAP)

            ready = split_ready_groups(pending, now, EVENT_MERGE_GAP, MAX_EVENT_SPAN)
            ready.sort(key=lambda g: g.first_start)

            if matched or ready:
                logger.info(
                    "Processing %d new review item(s), %d group(s) ready to send",
                    len(matched), len(ready),
                )

            # Sequential, not gather: keeps the chat feed in chronological order.
            for group in ready:
                try:
                    await send_grouped_notification(bot, group, http_client)
                except Exception as e:
                    logger.error("Fatal error processing notification group: %s", e)
    finally:
        # Always runs — see docstring. check_camera_health_and_alert is
        # itself guarded end-to-end, so this can never raise.
        await check_camera_health_and_alert(bot, http_client, now=now)

    return new_last_poll_ts


async def polling_loop(bot: Bot, http_client: httpx.AsyncClient) -> None:
    """Continuously poll Frigate for new events and send notifications.

    Implements a back-off strategy if Frigate is unreachable.
    """
    last_poll_ts = time.time()
    current_interval = POLLING_INTERVAL
    frigate_online = True
    pending: dict[str, PendingGroup] = {}

    logger.info(
        "Polling started — interval=%ds, cameras=%s",
        current_interval,
        list(MONITOR_CONFIG.keys()) if MONITOR_CONFIG else "all",
    )

    while True:
        try:
            notifications_enabled = state.enabled
            if not notifications_enabled:
                logger.debug("Notifications disabled — health check only this tick.")
                current_interval = POLLING_INTERVAL  # Reset interval while disabled

            try:
                # Always runs — camera-health alerts must not depend on
                # notifications being enabled (see _polling_tick docstring).
                last_poll_ts = await _polling_tick(
                    bot, http_client, pending, last_poll_ts, notifications_enabled=notifications_enabled
                )

                # Recovery logic
                if notifications_enabled and not frigate_online:
                    logger.info("Frigate is back online! Resuming normal polling.")
                    frigate_online = True
                    current_interval = POLLING_INTERVAL

            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if frigate_online:
                    logger.error("Frigate connection lost: %s. Entering back-off mode.", exc)
                    frigate_online = False

                # Simple linear back-off: increase interval but stay responsive
                current_interval = min(current_interval + 60, 300)
                logger.debug("Frigate unreachable, retrying in %ds", current_interval)

            except Exception as exc:
                logger.error("Unexpected error in polling loop: %s", exc, exc_info=DEBUG)
        except Exception as exc:
            logger.error("Critical failure in polling loop: %s", exc, exc_info=DEBUG)

        await asyncio.sleep(current_interval)



# ─────────────────────────── Entrypoint ──────────────────────────────


async def main() -> None:
    # Validate required config
    missing = []
    if not FRIGATE_URL:
        missing.append("FRIGATE_URL")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    logger.info("=== Frigate-Telegram Bot Starting ===")
    logger.info("Frigate URL: %s", mask_url(FRIGATE_URL))
    logger.info("External URL: %s", mask_url(EXTERNAL_URL) or "not configured")
    logger.info("Monitor config: %s", MONITOR_CONFIG if MONITOR_CONFIG else "all cameras/zones")
    logger.info("Polling interval: %ds", POLLING_INTERVAL)
    logger.info("Frigate timeout: %ds", FRIGATE_TIMEOUT)
    logger.info("Telegram connect timeout: %ds", TELEGRAM_CONNECT_TIMEOUT)
    logger.info("Upload timeout: %ds", UPLOAD_TIMEOUT)
    logger.info("Event merge gap: %ds", EVENT_MERGE_GAP)
    logger.info("Max event span: %ds", MAX_EVENT_SPAN)
    logger.info("Timezone: %s", TIMEZONE)
    logger.info("Debug: %s", DEBUG)

    # Build the Telegram application with command handlers
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["enable_notifications", "enable"], cmd_enable))
    app.add_handler(CommandHandler(["disable_notifications", "disable"], cmd_disable))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cameras", cmd_cameras))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("photo", cmd_photo))
    app.add_handler(CommandHandler("photo_all", cmd_photo_all))
    app.add_handler(CommandHandler("video", cmd_video))
    app.add_handler(CommandHandler("video_all", cmd_video_all))
    app.add_handler(CommandHandler("video_last", cmd_video_last))
    app.add_handler(CommandHandler("video_all_last", cmd_video_all_last))
    app.add_handler(CallbackQueryHandler(button_handler))

    async with httpx.AsyncClient() as http_client:
        # Store http_client in bot_data for use in command handlers
        app.bot_data["http_client"] = http_client

        # Check Frigate is reachable before starting
        if not await check_frigate_status(http_client):
            logger.error("Frigate is not reachable. Exiting.")
            sys.exit(1)

        # Initialize the Telegram application and start command polling
        await app.initialize()

        # Set command suggestions (autocomplete)
        commands = [
            BotCommand("menu", "Open the main interaction menu"),
            BotCommand("status", "Show bot configuration and health"),
            BotCommand("cameras", "List all registered cameras"),
            BotCommand("photo", "Get snapshot from a camera"),
            BotCommand("photo_all", "Get snapshots from all cameras"),
            BotCommand("video", "Record 30s manual clip"),
            BotCommand("video_all", "Record 30s clips from all cameras"),
            BotCommand("video_last", "Get last recorded event clip"),
            BotCommand("video_all_last", "Get last recorded clips for all cameras"),
            BotCommand("enable", "Turn on event alerts"),
            BotCommand("disable", "Turn off event alerts"),
            BotCommand("help", "Show help message"),
        ]
        await app.bot.set_my_commands(commands)

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot is active. Listening for commands.")

        try:
            await polling_loop(app.bot, http_client)
        except asyncio.CancelledError:
            logger.info("Polling loop cancelled.")
        finally:
            # Graceful shutdown
            logger.info("Shutting down…")
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


if __name__ == "__main__":
    # Handle SIGTERM/SIGINT for graceful Docker stops
    loop = asyncio.new_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received signal %s, shutting down…", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
