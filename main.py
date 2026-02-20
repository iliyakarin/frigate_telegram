"""
Frigate-Telegram Bot — Python 3.11+
Polls the Frigate HTTP API for detection events and sends rich notifications
to Telegram as a single animated GIF message with event details in the caption.
"""

import asyncio
import html
import json
import logging
import os
import signal
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Literal, Callable
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup, Bot, BotCommand
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

MONITOR_CONFIG_RAW = os.environ.get("MONITOR_CONFIG", "")

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
MEDIA_WAIT_TIMEOUT = get_int_setting("MEDIA_WAIT_TIMEOUT", 5)  # seconds to wait before fetching media
UPLOAD_TIMEOUT = get_int_setting("UPLOAD_TIMEOUT", 60)  # seconds for Telegram media upload (tunnel-safe)
SEND_CLIP = get_bool_setting("SEND_CLIP", False)  # send clip.mp4 instead of preview.gif for HD quality



# Media types configuration: { key: (filename, content_type) }
EVENT_MEDIA_CONFIG = {
    "gif": ("preview.gif", "image/gif"),
    "clip": ("clip.mp4", "video/mp4"),
    "thumbnail": ("thumbnail.jpg", "image/jpeg"),
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
    if not raw.strip():
        return {}

    config: dict[str, set[str]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            camera, zones_str = entry.split(":", 1)
            zones = {z.strip() for z in zones_str.split(",") if z.strip()}
            config[camera.strip()] = zones if zones else {"all"}
        else:
            # Camera name without zones → monitor all zones
            config[entry.strip()] = {"all"}
    return config


MONITOR_CONFIG = parse_monitor_config(MONITOR_CONFIG_RAW)

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
            self._path.write_text(json.dumps({"enabled": self._enabled}))

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
        logger.error("Cannot reach Frigate at %s: %s", FRIGATE_URL, exc)
        return False


async def fetch_events(client: httpx.AsyncClient, after_ts: float) -> list[dict]:
    """Fetch events from Frigate API for all monitored cameras since *after_ts*.

    If MONITOR_CONFIG is empty, fetches all cameras without filtering.
    Deduplicates events by ID across cameras.
    """
    async def fetch_camera_events(camera: str | None) -> list[dict]:
        params: dict[str, str | float] = {"after": after_ts}
        if camera:
            params["camera"] = camera
        try:
            resp = await client.get(
                f"{FRIGATE_URL}/api/events",
                params=params,
                auth=_http_auth(),
                timeout=FRIGATE_TIMEOUT,
            )
            resp.raise_for_status()
            events = resp.json()
            logger.debug("Fetched %d events for camera=%s", len(events), camera or "all")
            return events
        except Exception as exc:
            logger.warning("Error fetching events for camera=%s: %s", camera or "all", exc)
            return []

    cameras = list(MONITOR_CONFIG.keys()) if MONITOR_CONFIG else [None]
    results = await asyncio.gather(*[fetch_camera_events(c) for c in cameras])

    seen_ids: set[str] = set()
    all_events: list[dict] = []

    for events in results:
        for ev in events:
            eid = ev.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                all_events.append(ev)

    return all_events


async def fetch_media_with_retry(
    client: httpx.AsyncClient,
    url: str,
    label: str,
    expected_content_type: str | None = None,
) -> bytes | None:
    """Fetch media from a URL with retry logic for 404/transient errors.

    Args:
        url: Full URL to fetch.
        label: Human-readable label for logging (e.g. 'preview.gif for event X').
        expected_content_type: If set, warn when the response Content-Type
            doesn't match (helps detect octet-stream issues).

    Returns:
        Raw bytes or None if all retries failed.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.get(url, auth=_http_auth(), timeout=FRIGATE_TIMEOUT)

            if DEBUG:
                logger.debug("Fetching Frigate API: %s %s", resp.status_code, url)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if resp.status_code == 404:
                # 404 means Frigate hasn't generated the media yet — retry
                if attempt < MAX_RETRIES:
                    logger.debug("%s: media not ready (404), retry %d/%d", label, attempt, MAX_RETRIES)
                else:
                    logger.warning("%s: media not found (404) after %d attempts. URL: %s", label, MAX_RETRIES, url)
            else:
                logger.error("%s: HTTP error %d: %s. URL: %s", label, resp.status_code, exc, url)
            
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
        except httpx.RequestError as exc:
            logger.error("%s: Network error: %s", label, exc)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
                continue
            return None
        except Exception as exc:
            logger.error("%s: Unexpected error fetching %s: %s", label, url, exc)
            return None

        # Success path
        try:
            # Verify basic response validity
            if len(resp.content) < 100:
                logger.warning("%s: response too small (%d bytes), retrying", label, len(resp.content))
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return None

            # Verify content type if expected
            ct = resp.headers.get("content-type", "").lower()
            if expected_content_type and expected_content_type.lower() not in ct:
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
) -> bytes | None:
    """Internal helper to fetch from Frigate API."""
    url = f"{FRIGATE_URL}/api/{path}"
    return await fetch_media_with_retry(client, url, label, expected_content_type)


async def fetch_event_details(client: httpx.AsyncClient, event_id: str) -> dict | None:
    """Fetch full event details from Frigate API."""
    try:
        resp = await client.get(
            f"{FRIGATE_URL}/api/events/{event_id}",
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
    media_type: Literal["gif", "clip", "thumbnail"],
) -> bytes | None:
    """Fetch event-related media (gif, clip, or thumbnail)."""
    filename, content_type = EVENT_MEDIA_CONFIG[media_type]
    return await _fetch_frigate_api(
        client,
        f"events/{event_id}/{filename}",
        f"{filename} for {event_id}",
        content_type,
    )




async def fetch_camera_snapshot(client: httpx.AsyncClient, camera: str) -> bytes | None:
    """Fetch the latest snapshot JPEG from a camera."""
    safe_camera = urllib.parse.quote(camera)
    return await _fetch_frigate_api(
        client,
        f"{safe_camera}/latest.jpg?bbox=1",
        f"latest.jpg for {camera}",
        "image/jpeg",
    )


async def fetch_camera_list(client: httpx.AsyncClient) -> list[str]:
    """Fetch the list of camera names from Frigate API."""
    try:
        # /api/config contains the full configuration including cameras
        resp = await client.get(f"{FRIGATE_URL}/api/config", auth=_http_auth(), timeout=FRIGATE_TIMEOUT)
        resp.raise_for_status()
        config = resp.json()
        cameras = list(config.get("cameras", {}).keys())
        return sorted(cameras)
    except Exception as exc:
        logger.error("Error fetching camera list: %s", exc)
        return []


async def fetch_recording_clip(
    client: httpx.AsyncClient, camera: str, start_ts: int, end_ts: int
) -> bytes | None:
    """Fetch a recording clip for a specific time range."""
    # Frigate API: /api/<camera_name>/start/<start_ts>/end/<end_ts>/clip.mp4
    safe_camera = urllib.parse.quote(camera)
    return await _fetch_frigate_api(
        client,
        f"{safe_camera}/start/{start_ts}/end/{end_ts}/clip.mp4",
        f"clip.mp4 for {camera} ({start_ts}-{end_ts})",
        "video/mp4",
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
        for i in range(5):
            data = await fetch_event_media(client, event_id, "clip")
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
        url = f"{FRIGATE_URL}/api/events/{urllib.parse.quote(camera)}/{urllib.parse.quote(label)}/create"
        params = {"include_recording": "1", "duration": str(duration)}
        
        if DEBUG:
            logger.debug("Triggering manual event: %s params=%s", url, params)

        resp = await client.post(url, params=params, auth=_http_auth(), timeout=FRIGATE_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("event_id")
    except Exception as exc:
        logger.error("Error triggering manual event for %s: %s", camera, exc)
        return None


# ─────────────────────── Event Filtering ─────────────────────────────


def event_matches_config(event: dict) -> bool:
    """Check whether an event matches the MONITOR_CONFIG zones filter.

    If MONITOR_CONFIG is empty, all events pass.
    """
    if not MONITOR_CONFIG:
        return True

    camera = event.get("camera", "")
    if camera not in MONITOR_CONFIG:
        return False

    allowed_zones = MONITOR_CONFIG[camera]
    if "all" in allowed_zones:
        return True

    event_zones = event.get("zones", [])
    # Match if any event zone is in the allowed list
    return not allowed_zones.isdisjoint(event_zones)


# ─────────────────────── Caption Formatting ──────────────────────────


def _epoch_to_datetime(epoch: float | None) -> str:
    """Convert epoch timestamp to a human-readable datetime string."""
    if epoch is None or epoch == 0:
        return "N/A"
    try:
        tz = ZoneInfo(TIMEZONE)
        dt = datetime.fromtimestamp(epoch, tz=tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def format_caption(event: dict) -> str:
    """Build an HTML caption for the Telegram animation message.

    Includes face recognition sub_label when available from Frigate.
    Handles sub_label as [name, score] array, plain string, or dictionary.
    """
    event_id = event.get("id", "unknown")
    camera = event.get("camera", "unknown")
    label = event.get("label", "object")
    raw_sub_label = event.get("sub_label")
    # Fallback to data field if top-level sub_label is missing
    if not raw_sub_label and "data" in event:
        raw_sub_label = event.get("data", {}).get("sub_label")

    zones = ", ".join(event.get("zones", [])) or "N/A"
    score = event.get("top_score")
    score_str = f"{score:.0%}" if score else "N/A"
    start_time = _epoch_to_datetime(event.get("start_time"))
    end_time = _epoch_to_datetime(event.get("end_time"))

    # Parse sub_label — Frigate returns either ["name", score], plain string, or dictionary
    sub_label_name = None
    sub_label_score = None
    if isinstance(raw_sub_label, list) and len(raw_sub_label) >= 1:
        sub_label_name = str(raw_sub_label[0])
        if len(raw_sub_label) >= 2:
            try:
                sub_label_score = float(raw_sub_label[1])
            except (ValueError, TypeError):
                pass
    elif isinstance(raw_sub_label, dict):
        sub_label_name = raw_sub_label.get("label") or raw_sub_label.get("name")
        sub_label_score = raw_sub_label.get("score")
    elif isinstance(raw_sub_label, str) and raw_sub_label:
        sub_label_name = raw_sub_label

    if DEBUG and raw_sub_label:
        logger.debug("Event %s raw sub_label: %s", event_id, raw_sub_label)

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



# ─────────────────────── Telegram Notification ───────────────────────


async def send_event_notification(bot: Bot, event: dict, http_client: httpx.AsyncClient) -> None:
    """Send a **single** consolidated Telegram message for a Frigate event.

    Flow:
    1. Wait MEDIA_WAIT_TIMEOUT seconds so Frigate can generate the preview.
    2. Refetch event details to get the latest metadata (like sub_label).
    3. Fetch clip/GIF, thumbnail, and camera snapshot in parallel.
    4. Send ONE message:
       - SEND_CLIP + clip → send_video (HD clip.mp4)
       - GIF available    → send_animation (preview.gif)
       - No GIF, photo    → send_photo (snapshot/thumbnail)
       - No media at all  → send_message (text-only fallback)

    Media is wrapped with an explicit filename so Telegram recognises the
    Content-Type correctly (fixes the octet-stream / broken-file issue).
    """
    event_id = event.get("id", "unknown")
    camera = event.get("camera", "unknown")

    # ── Wait for Frigate to generate previews ────────────────────────
    if MEDIA_WAIT_TIMEOUT > 0:
        logger.debug(
            "Event %s: waiting %ds for Frigate to generate media…",
            event_id, MEDIA_WAIT_TIMEOUT,
        )
        await asyncio.sleep(MEDIA_WAIT_TIMEOUT)

    # ── Refetch event details to get updated sub_label ────────────────
    updated_event = await fetch_event_details(http_client, event_id)
    if updated_event:
        event = updated_event

    caption = format_caption(event)

    # ── Fetch all media in parallel ──────────────────────────────────
    gif_task = asyncio.create_task(fetch_event_media(http_client, event_id, "gif"))
    thumb_task = asyncio.create_task(fetch_event_media(http_client, event_id, "thumbnail"))
    snap_task = asyncio.create_task(fetch_camera_snapshot(http_client, camera))
    clip_task = (
        asyncio.create_task(fetch_event_media(http_client, event_id, "clip"))
        if SEND_CLIP
        else None
    )

    gif_data, thumb_data, snap_data = await asyncio.gather(gif_task, thumb_task, snap_task)
    clip_data = await clip_task if clip_task else None

    # Choose the best available photo (snapshot is higher quality than thumbnail)
    photo_data = snap_data or thumb_data

    try:
        if SEND_CLIP and clip_data:
            # ── HD: send clip.mp4 as video (auto-plays in Telegram) ───
            await bot.send_video(
                chat_id=TELEGRAM_CHAT_ID,
                video=clip_data,
                thumbnail=photo_data,
                caption=caption,
                parse_mode=ParseMode.HTML,
                filename="clip.mp4",
                supports_streaming=True,
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
            )
            logger.info("Event %s → sent HD video clip with caption ✓", event_id)

        elif gif_data:
            # ── Standard: send preview.gif as animation ───────────────
            await bot.send_animation(
                chat_id=TELEGRAM_CHAT_ID,
                animation=gif_data,
                thumbnail=photo_data,
                caption=caption,
                parse_mode=ParseMode.HTML,
                filename="preview.gif",
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
            )
            logger.info("Event %s → sent animation with caption ✓", event_id)

        elif photo_data:
            # ── Fallback 1: send photo with caption ──────────────────
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo_data,
                caption=caption,
                parse_mode=ParseMode.HTML,
                filename="snapshot.jpg",
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
            )
            logger.info("Event %s → sent photo with caption (GIF unavailable)", event_id)

        else:
            # ── Fallback 2: text-only ────────────────────────────────
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
            )
            logger.info("Event %s → sent text only (no media available)", event_id)

    except Exception as exc:
        logger.error("Failed to send Telegram notification for event %s: %s", event_id, exc)


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
    lines = [
        "📊 <b>Bot Status</b>",
        "",
        f"<b>Notifications:</b> {status_emoji} {status_text}",
        f"<b>Polling Interval:</b> ⏱ {POLLING_INTERVAL}s",
        f"<b>Monitored Cameras:</b> 🎥 {html.escape(cameras)}",
        "",
        "🛠 <b>Configuration</b>",
        f"<b>Frigate URL:</b> 🔗 {html.escape(FRIGATE_URL)}",
        f"<b>External URL:</b> 🌐 {html.escape(EXTERNAL_URL) if EXTERNAL_URL else 'Not configured'}",
        f"<b>Frigate Timeout:</b> ⏳ {FRIGATE_TIMEOUT}s",
        f"<b>Upload Timeout:</b> 📤 {UPLOAD_TIMEOUT}s",
    ]
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
        lines.append(f"• <code>{html.escape(cam)}</code>")

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
        read_timeout=UPLOAD_TIMEOUT,
        write_timeout=UPLOAD_TIMEOUT,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
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
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
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
            read_timeout=UPLOAD_TIMEOUT,
            write_timeout=UPLOAD_TIMEOUT,
            connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
        )
    except Exception as e:
        logger.error(f"Error in cmd_video for {camera_name}: {e}", exc_info=True)
        await update.effective_chat.send_message(f"⚠️ Error recording video for {camera_name}: {str(e)}")


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
                    read_timeout=UPLOAD_TIMEOUT,
                    write_timeout=UPLOAD_TIMEOUT,
                    connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
                )
            else:
                await update.effective_chat.send_message(f"❌ Failed to fetch video clip for <code>{html.escape(camera)}</code> (Event {event_id})", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error in fetch_and_send for {camera}: {e}", exc_info=True)
            await update.effective_chat.send_message(f"⚠️ Error fetching video for {camera}: {str(e)}")

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
        read_timeout=UPLOAD_TIMEOUT,
        write_timeout=UPLOAD_TIMEOUT,
        connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
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
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=TELEGRAM_CONNECT_TIMEOUT,
            )
        else:
            await update.effective_chat.send_message(f"❌ Failed to fetch video for any of the last {len(events)} events on <code>{html.escape(camera)}</code>", parse_mode=ParseMode.HTML)

    await asyncio.gather(*[fetch_and_send(cam) for cam in cameras])


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


async def polling_loop(bot: Bot, http_client: httpx.AsyncClient) -> None:
    """Continuously poll Frigate for new events and send notifications.
    
    Implements a back-off strategy if Frigate is unreachable.
    """
    last_poll_ts = time.time()
    current_interval = POLLING_INTERVAL
    frigate_online = True

    logger.info(
        "Polling started — interval=%ds, cameras=%s",
        current_interval,
        list(MONITOR_CONFIG.keys()) if MONITOR_CONFIG else "all",
    )

    while True:
        try:
            if state.enabled:
                poll_after = last_poll_ts
                
                try:
                    events = await fetch_events(http_client, poll_after)
                    
                    # Recovery logic
                    if not frigate_online:
                        logger.info("Frigate is back online! Resuming normal polling.")
                        frigate_online = True
                        current_interval = POLLING_INTERVAL

                    last_poll_ts = time.time()
                    matched = [ev for ev in events if event_matches_config(ev)]

                    if matched:
                        logger.info("Processing %d new event(s)", len(matched))
                        for event in matched:
                            try:
                                await send_event_notification(bot, event, http_client)
                            except Exception as e:
                                logger.error("Fatal error processing event notification: %s", e)
                        logger.info("All events processed.")
                    else:
                        logger.debug("No new matching events.")
                
                except (httpx.NetworkError, httpx.TimeoutException) as exc:
                    if frigate_online:
                        logger.error("Frigate connection lost: %s. Entering back-off mode.", exc)
                        frigate_online = False
                    
                    # Simple linear back-off: increase interval but stay responsive
                    current_interval = min(current_interval + 60, 300) 
                    logger.debug("Frigate unreachable, retrying in %ds", current_interval)
                
                except Exception as exc:
                    logger.error("Unexpected error in polling loop: %s", exc, exc_info=DEBUG)
            else:
                logger.debug("Notifications disabled — skipping poll.")
                current_interval = POLLING_INTERVAL # Reset interval while disabled
        except Exception as exc:
            logger.error("Critical failure in polling loop: %s", exc, exc_info=True)

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
    logger.info("Frigate URL: %s", FRIGATE_URL)
    logger.info("External URL: %s", EXTERNAL_URL or "not configured")
    logger.info("Monitor config: %s", MONITOR_CONFIG if MONITOR_CONFIG else "all cameras/zones")
    logger.info("Polling interval: %ds", POLLING_INTERVAL)
    logger.info("Frigate timeout: %ds", FRIGATE_TIMEOUT)
    logger.info("Telegram connect timeout: %ds", TELEGRAM_CONNECT_TIMEOUT)
    logger.info("Media wait timeout: %ds", MEDIA_WAIT_TIMEOUT)
    logger.info("Upload timeout: %ds", UPLOAD_TIMEOUT)
    logger.info("Send HD clip: %s", SEND_CLIP)
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
