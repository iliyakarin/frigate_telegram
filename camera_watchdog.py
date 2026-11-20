"""Camera Watchdog: auto-reboot Amcrest/Dahua cameras on sustained 0 FPS."""

import asyncio
from dataclasses import dataclass
import html
import logging
import os
import signal
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv
import httpx

from camera_health import format_downtime, parse_monitored_cameras

logger = logging.getLogger("frigate-watchdog")

DEFAULT_ZERO_FPS_THRESHOLD_SECONDS = 300
DEFAULT_REBOOT_COOLDOWN_SECONDS = 900
DEFAULT_POLL_INTERVAL_SECONDS = 30
FRIGATE_STARTUP_GRACE_SECONDS = 60
FPS_FAILURE_THRESHOLD = 0.1
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_STREAM_SCHEMES = frozenset({"rtsp", "rtsps", "http", "https", "rtmp", "onvif"})


def get_int_setting(key: str, default: int, min_value: int = 1) -> int:
    """Safely parse integer environment variable with fallback and minimum value check."""
    val = os.getenv(key)
    if val is None or val.strip() == "":
        return default
    try:
        parsed = int(val)
    except ValueError:
        logger.warning("Invalid integer for %s: '%s'. Using default: %d", key, val, default)
        return default
    if parsed < min_value:
        logger.warning(
            "Value for %s (%d) is below minimum (%d). Using default: %d",
            key,
            parsed,
            min_value,
            default,
        )
        return default
    return parsed


def _clean_stream_url(raw_url: str) -> str | None:
    """Normalize a Frigate/go2rtc stream URL so urllib.parse.urlsplit can parse it safely."""
    if not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    if cleaned.startswith("ffmpeg:"):
        cleaned = cleaned[len("ffmpeg:") :].strip()
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0].strip()
    if "://" not in cleaned:
        return None

    scheme, _, remainder = cleaned.partition("://")
    authority, slash, path_part = remainder.partition("/")
    if "@" in authority:
        _, _, hostport = authority.rpartition("@")
        authority = f"user@{hostport}"
    return f"{scheme}://{authority}{slash}{path_part}"


def extract_host_from_stream_url(raw_url: str) -> str | None:
    """Extract lowercase hostname/IP from a Frigate or go2rtc stream URL."""
    cleaned = _clean_stream_url(raw_url)
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ALLOWED_STREAM_SCHEMES:
        return None
    return parsed.hostname.lower() if parsed.hostname else None


def extract_go2rtc_stream_name(raw_url: str) -> str | None:
    """Extract the go2rtc stream name (last path segment) from a restream URL."""
    cleaned = _clean_stream_url(raw_url)
    if not cleaned:
        return None
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return None
    stripped_path = parsed.path.strip("/")
    if not stripped_path:
        return None
    return stripped_path.split("/")[-1]


def resolve_camera_ips(
    config_data: dict[str, Any],
    cameras: list[str] | set[str] | None = None,
) -> dict[str, str]:
    """Resolve physical camera IPs/hostnames from Frigate's /api/config payload."""
    if not isinstance(config_data, dict):
        return {}

    go2rtc_cfg = config_data.get("go2rtc")
    go2rtc_streams = (
        go2rtc_cfg.get("streams")
        if isinstance(go2rtc_cfg, dict) and isinstance(go2rtc_cfg.get("streams"), dict)
        else {}
    )

    def _resolve_go2rtc_stream_host(
        stream_name: str, visited: set[str] | None = None
    ) -> str | None:
        if not stream_name:
            return None
        if visited is None:
            visited = set()
        if stream_name in visited:
            return None
        visited.add(stream_name)

        entry = go2rtc_streams.get(stream_name)
        if isinstance(entry, str):
            sources = [entry]
        elif isinstance(entry, list):
            sources = [item for item in entry if isinstance(item, str)]
        else:
            return None

        for src in sources:
            host = extract_host_from_stream_url(src)
            if not host:
                continue
            if host in LOOPBACK_HOSTS:
                next_stream = extract_go2rtc_stream_name(src)
                if next_stream and next_stream not in visited:
                    resolved_chain = _resolve_go2rtc_stream_host(next_stream, visited)
                    if resolved_chain:
                        return resolved_chain
            else:
                return host
        return None

    cameras_dict = config_data.get("cameras")
    if not isinstance(cameras_dict, dict):
        return {}

    target_filter = set(cameras) if cameras is not None else None
    resolved: dict[str, str] = {}

    for cam, cam_cfg in cameras_dict.items():
        if target_filter is not None and cam not in target_filter:
            continue
        if not isinstance(cam_cfg, dict):
            continue

        ffmpeg_cfg = cam_cfg.get("ffmpeg")
        raw_inputs = (
            ffmpeg_cfg.get("inputs")
            if isinstance(ffmpeg_cfg, dict) and isinstance(ffmpeg_cfg.get("inputs"), list)
            else []
        )
        valid_inputs = [inp for inp in raw_inputs if isinstance(inp, dict)]
        detect_inputs = [
            inp
            for inp in valid_inputs
            if isinstance(inp.get("roles"), list) and "detect" in inp.get("roles")
        ]
        other_inputs = [inp for inp in valid_inputs if inp not in detect_inputs]

        cam_ip: str | None = None
        for inp in detect_inputs + other_inputs:
            path = inp.get("path")
            if not isinstance(path, str):
                continue
            host = extract_host_from_stream_url(path)
            if not host:
                continue
            if host in LOOPBACK_HOSTS:
                stream_name = extract_go2rtc_stream_name(path)
                if stream_name:
                    cam_ip = _resolve_go2rtc_stream_host(stream_name)
                    if cam_ip:
                        break
            else:
                cam_ip = host
                break

        if not cam_ip:
            cam_ip = _resolve_go2rtc_stream_host(cam) or _resolve_go2rtc_stream_host(f"{cam}_sub")

        if cam_ip:
            resolved[cam] = cam_ip

    return resolved


@dataclass
class CameraWatchdogState:
    """Per-camera 0-FPS tracking and reboot cooldown state."""

    first_zero_ts: float | None = None
    last_reboot_ts: float | None = None


@dataclass(frozen=True)
class RebootAction:
    """Action indicating a camera has exceeded the 0-FPS threshold and should be rebooted."""

    camera: str
    zero_duration_seconds: float


class CameraWatchdog:
    """State machine tracking sustained 0-FPS cameras and reboot cooldowns."""

    def __init__(
        self,
        monitored_cameras: list[str] | None,
        zero_fps_threshold: float = DEFAULT_ZERO_FPS_THRESHOLD_SECONDS,
        cooldown_seconds: float = DEFAULT_REBOOT_COOLDOWN_SECONDS,
    ) -> None:
        self._monitored: set[str] | None = (
            set(monitored_cameras) if monitored_cameras is not None else None
        )
        self._zero_fps_threshold = float(zero_fps_threshold)
        self._cooldown_seconds = float(cooldown_seconds)
        self._states: dict[str, CameraWatchdogState] = {}
        self.cached_ips: dict[str, str] = {}

    def evaluate_stats(
        self,
        stats_data: dict[str, Any],
        now: float | None = None,
    ) -> list[RebootAction]:
        """Evaluate Frigate /api/stats payload and return any due RebootActions."""
        if now is None:
            now = time.time()

        if not isinstance(stats_data, dict):
            return []

        service = stats_data.get("service")
        service = service if isinstance(service, dict) else {}
        uptime = service.get("uptime")
        if (
            not (isinstance(uptime, (int, float)) and not isinstance(uptime, bool))
            or uptime < FRIGATE_STARTUP_GRACE_SECONDS
        ):
            for state in self._states.values():
                state.first_zero_ts = None
            return []

        cameras_stats = stats_data.get("cameras")
        if not isinstance(cameras_stats, dict):
            return []

        actions: list[RebootAction] = []
        for camera, data in cameras_stats.items():
            if self._monitored is not None and camera not in self._monitored:
                continue

            data = data if isinstance(data, dict) else {}
            raw_fps = data.get("camera_fps")
            current_fps = (
                float(raw_fps)
                if isinstance(raw_fps, (int, float)) and not isinstance(raw_fps, bool)
                else 0.0
            )

            state = self._states.setdefault(camera, CameraWatchdogState())
            if current_fps >= FPS_FAILURE_THRESHOLD:
                state.first_zero_ts = None
                continue

            if state.first_zero_ts is None:
                state.first_zero_ts = now
                continue

            zero_duration = now - state.first_zero_ts
            if zero_duration < self._zero_fps_threshold:
                continue

            if (
                state.last_reboot_ts is not None
                and (now - state.last_reboot_ts) < self._cooldown_seconds
            ):
                continue

            actions.append(
                RebootAction(camera=camera, zero_duration_seconds=zero_duration)
            )

        return actions

    def mark_rebooted(self, camera: str, now: float | None = None) -> None:
        """Record that a camera was just rebooted at `now`."""
        if now is None:
            now = time.time()
        state = self._states.setdefault(camera, CameraWatchdogState())
        state.last_reboot_ts = now
        state.first_zero_ts = now


async def fetch_frigate_json(
    client: httpx.AsyncClient,
    frigate_url: str,
    path: str,
    auth: httpx.Auth | tuple[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """Fetch a JSON dictionary from a Frigate API endpoint."""
    url = f"{frigate_url.rstrip('/')}{path}"
    try:
        resp = await client.get(url, auth=auth, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("Frigate API %s returned HTTP %d", path, resp.status_code)
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Failed to fetch Frigate API %s: %s", path, type(exc).__name__)
        return None


async def reboot_camera_http(
    client: httpx.AsyncClient,
    ip: str,
    username: str,
    password: str,
    timeout: float = 10.0,
) -> bool:
    """Trigger an HTTP Digest-authenticated reboot on an Amcrest/Dahua camera."""
    url = f"http://{ip}/cgi-bin/magicBox.cgi"
    try:
        resp = await client.get(
            url,
            params={"action": "reboot"},
            auth=httpx.DigestAuth(username, password),
            timeout=timeout,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Reboot request to %s returned HTTP %d", ip, resp.status_code)
        return False
    except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
        req = getattr(exc, "request", None)
        if req is not None and "authorization" in req.headers:
            logger.info(
                "Camera %s closed connection after authenticated reboot request (%s); treating as success",
                ip,
                type(exc).__name__,
            )
            return True
        logger.warning(
            "Connection dropped before authentication completed for camera %s (%s)",
            ip,
            type(exc).__name__,
        )
        return False
    except httpx.HTTPError as exc:
        logger.warning("HTTP error rebooting camera %s: %s", ip, type(exc).__name__)
        return False


def format_reboot_message(
    camera: str,
    ip: str,
    zero_duration_seconds: float,
    cooldown_seconds: float = DEFAULT_REBOOT_COOLDOWN_SECONDS,
) -> str:
    """Format an HTML Telegram message for an auto-rebooted camera."""
    safe_cam = html.escape(camera)
    safe_ip = html.escape(ip)
    return (
        "🔄 <b>Camera Auto-Rebooted</b>\n\n"
        f"📹 <b>Camera:</b> <code>{safe_cam}</code> (<code>{safe_ip}</code>)\n"
        f"⏱ <b>Zero FPS duration:</b> {format_downtime(zero_duration_seconds)}\n"
        f"🛡 <b>Cooldown:</b> {format_downtime(cooldown_seconds)}"
    )


async def send_telegram_alert(
    client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: float = 15.0,
) -> bool:
    """Send an HTML message via the Telegram Bot API without leaking bot_token in logs."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = await client.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Telegram sendMessage failed with HTTP %d", resp.status_code)
        return False
    except httpx.HTTPError as exc:
        logger.warning("Telegram sendMessage failed: %s", type(exc).__name__)
        return False


async def run_watchdog_tick(
    client: httpx.AsyncClient,
    watchdog: CameraWatchdog,
    frigate_url: str,
    rtsp_user: str,
    rtsp_password: str,
    frigate_auth: httpx.Auth | tuple[str, str] | None = None,
    bot_token: str = "",
    chat_id: str = "",
    ip_overrides: dict[str, str] | None = None,
    now: float | None = None,
    timeout: float = 15.0,
) -> list[str]:
    """Execute one watchdog poll tick and return list of camera names rebooted."""
    if now is None:
        now = time.time()

    stats = await fetch_frigate_json(
        client, frigate_url, "/api/stats", auth=frigate_auth, timeout=timeout
    )
    if not stats:
        return []

    actions = watchdog.evaluate_stats(stats, now=now)
    if not actions:
        return []

    config = await fetch_frigate_json(
        client, frigate_url, "/api/config", auth=frigate_auth, timeout=timeout
    )
    if config:
        watchdog.cached_ips.update(resolve_camera_ips(config))

    ip_map = {**watchdog.cached_ips, **(ip_overrides or {})}
    rebooted: list[str] = []

    for action in actions:
        ip = ip_map.get(action.camera)
        if not ip:
            logger.warning(
                "Cannot reboot %s: unable to resolve camera IP from Frigate config",
                action.camera,
            )
            continue

        ok = await reboot_camera_http(
            client, ip, rtsp_user, rtsp_password, timeout=timeout
        )
        if ok:
            watchdog.mark_rebooted(action.camera, now=now)
            logger.info(
                "Auto-rebooted camera %s (%s) after %s at 0 FPS",
                action.camera,
                ip,
                format_downtime(action.zero_duration_seconds),
            )
            if bot_token and chat_id:
                msg = format_reboot_message(
                    action.camera,
                    ip,
                    action.zero_duration_seconds,
                    watchdog._cooldown_seconds,
                )
                await send_telegram_alert(
                    client, bot_token, chat_id, msg, timeout=timeout
                )
            rebooted.append(action.camera)

    return rebooted


async def main() -> None:
    """Entrypoint for the standalone camera-watchdog service."""
    load_dotenv()
    debug = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    frigate_url = os.getenv("FRIGATE_URL", "http://frigate:5000").rstrip("/")
    frigate_user = os.getenv("FRIGATE_USERNAME", "")
    frigate_pass = os.getenv("FRIGATE_PASSWORD", "")
    frigate_auth = (
        httpx.BasicAuth(frigate_user, frigate_pass)
        if frigate_user and frigate_pass
        else None
    )

    rtsp_user = os.getenv("FRIGATE_RTSP_USER", "").strip()
    rtsp_password = os.getenv("FRIGATE_RTSP_PASSWORD", "").strip()
    if not rtsp_user or not rtsp_password:
        logger.error(
            "FRIGATE_RTSP_USER and FRIGATE_RTSP_PASSWORD must be set for camera-watchdog"
        )
        sys.exit(1)

    monitored = parse_monitored_cameras(os.getenv("WATCHDOG_CAMERAS"))
    if not monitored:
        logger.error(
            "WATCHDOG_CAMERAS must be set to a comma-separated or JSON list of cameras to watch"
        )
        sys.exit(1)

    zero_fps_seconds = get_int_setting(
        "WATCHDOG_ZERO_FPS_SECONDS", DEFAULT_ZERO_FPS_THRESHOLD_SECONDS, min_value=1
    )
    cooldown_seconds = get_int_setting(
        "WATCHDOG_COOLDOWN_SECONDS", DEFAULT_REBOOT_COOLDOWN_SECONDS, min_value=1
    )
    poll_interval = get_int_setting(
        "WATCHDOG_POLL_INTERVAL", DEFAULT_POLL_INTERVAL_SECONDS, min_value=1
    )
    frigate_timeout = get_int_setting("FRIGATE_TIMEOUT", 15, min_value=1)

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    watchdog = CameraWatchdog(
        monitored_cameras=monitored,
        zero_fps_threshold=zero_fps_seconds,
        cooldown_seconds=cooldown_seconds,
    )

    logger.info(
        "Starting camera-watchdog for cameras=%s (threshold=%ds, cooldown=%ds, poll=%ds)",
        monitored,
        zero_fps_seconds,
        cooldown_seconds,
        poll_interval,
    )

    async with httpx.AsyncClient() as client:
        config = await fetch_frigate_json(
            client, frigate_url, "/api/config", auth=frigate_auth, timeout=frigate_timeout
        )
        if config:
            watchdog.cached_ips.update(resolve_camera_ips(config, cameras=monitored))
            logger.info("Resolved camera IPs at startup: %s", watchdog.cached_ips)
            for cam in monitored:
                if cam not in watchdog.cached_ips:
                    logger.warning(
                        "Could not resolve IP for watched camera '%s' at startup", cam
                    )

        while True:
            await run_watchdog_tick(
                client,
                watchdog,
                frigate_url=frigate_url,
                rtsp_user=rtsp_user,
                rtsp_password=rtsp_password,
                frigate_auth=frigate_auth,
                bot_token=bot_token,
                chat_id=chat_id,
                timeout=frigate_timeout,
            )
            await asyncio.sleep(poll_interval)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received signal %s, shutting down...", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for _sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(_sig, _shutdown, _sig)

    try:
        loop.run_until_complete(main())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
