"""Camera health monitoring, debounce state machine, and alerting for Frigate cameras."""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Repeat escalation delays in seconds after the preceding alert:
# Alert 1: Initial (after debounce)
# Alert 2: +5 minutes (300s)
# Alert 3: +60 minutes (3600s)
# Alert 4: +12 hours (43200s)
# Alert 5: +24 hours (86400s)
ESCALATION_DELAYS_SECONDS: list[float] = [
    300.0,    # Repeat 1: +5 min
    3600.0,   # Repeat 2: +60 min
    43200.0,  # Repeat 3: +12 hours
    86400.0,  # Repeat 4: +24 hours
]

MAX_HEALTH_ALERTS: int = 1 + len(ESCALATION_DELAYS_SECONDS)  # 5 total alerts


def parse_monitored_cameras(val: str | None) -> list[str] | None:
    """Parse HEALTH_MONITOR_CAMERAS into a list of camera names.

    Supports JSON array ('["front", "back"]') or comma-separated ('front, back').
    Returns None if unset or empty (meaning monitor all cameras).
    """
    if not val:
        return None
    cleaned = val.strip()
    if not cleaned:
        return None

    if cleaned.startswith("[") and cleaned.endswith("]"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    items = [item.strip() for item in cleaned.split(",") if item.strip()]
    return items if items else None


def format_downtime(seconds: float) -> str:
    """Format duration in seconds into human-readable string like '1h 2m 3s'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"

    days, remainder = divmod(s, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 and days == 0:
        parts.append(f"{secs}s")

    return " ".join(parts) if parts else "0s"


@dataclass
class HealthAlert:
    camera: str
    alert_type: str  # "offline" or "recovery"
    alert_count: int
    message: str


@dataclass
class CameraHealthState:
    camera: str
    is_offline: bool = False
    notified_offline: bool = False
    first_failure_ts: float | None = None
    last_alert_ts: float | None = None
    alert_count: int = 0
    last_error_detail: str | None = None
    current_fps: float = 0.0
    expected_fps: float = 0.0


class CameraHealthMonitor:
    """Tracks camera health, enforces debounce, coordinates repeat alerts, and handles recovery."""

    def __init__(
        self,
        monitored_cameras: list[str] | None = None,
        debounce_seconds: float = 60.0,
    ) -> None:
        self.monitored_cameras = monitored_cameras
        self.debounce_seconds = debounce_seconds
        self.states: dict[str, CameraHealthState] = {}

    def should_monitor(self, camera: str) -> bool:
        """Whether the specified camera should be monitored for health."""
        if self.monitored_cameras is None:
            return True
        return camera in self.monitored_cameras

    def _get_state(self, camera: str) -> CameraHealthState:
        if camera not in self.states:
            self.states[camera] = CameraHealthState(camera=camera)
        return self.states[camera]

    def update_camera(
        self,
        camera: str,
        is_failing: bool,
        current_fps: float = 0.0,
        expected_fps: float = 0.0,
        error_detail: str | None = None,
        now: float | None = None,
    ) -> HealthAlert | None:
        """Update a camera's health status and return an alert if due."""
        if not self.should_monitor(camera):
            return None

        if now is None:
            now = time.time()

        state = self._get_state(camera)
        state.current_fps = current_fps
        state.expected_fps = expected_fps
        if error_detail:
            state.last_error_detail = error_detail

        # Case 1: Camera is Healthy
        if not is_failing:
            if state.notified_offline:
                # Camera was previously offline and notified; send recovery alert
                downtime = now - (state.first_failure_ts or now)
                msg = self.format_recovery_alert(
                    camera=camera,
                    current_fps=current_fps,
                    downtime_seconds=downtime,
                    timestamp_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                )
                alert = HealthAlert(
                    camera=camera,
                    alert_type="recovery",
                    alert_count=state.alert_count,
                    message=msg,
                )
                # Reset state completely
                state.is_offline = False
                state.notified_offline = False
                state.first_failure_ts = None
                state.last_alert_ts = None
                state.alert_count = 0
                state.last_error_detail = None
                return alert
            else:
                # Normal healthy state or recovered before debounce elapsed
                state.is_offline = False
                state.first_failure_ts = None
                return None

        # Case 2: Camera is Failing
        state.is_offline = True
        if state.first_failure_ts is None:
            state.first_failure_ts = now

        offline_duration = now - state.first_failure_ts

        # Debounce check for the first alert
        if state.alert_count == 0:
            if offline_duration >= self.debounce_seconds:
                # Debounce elapsed; send initial Alert 1
                state.alert_count = 1
                state.last_alert_ts = now
                state.notified_offline = True

                msg = self.format_offline_alert(
                    camera=camera,
                    current_fps=current_fps,
                    expected_fps=expected_fps,
                    alert_count=state.alert_count,
                    error_detail=state.last_error_detail,
                    timestamp_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                )
                return HealthAlert(
                    camera=camera,
                    alert_type="offline",
                    alert_count=state.alert_count,
                    message=msg,
                )
            return None

        # Escalation check for repeat alerts (Alerts 2 to 5)
        if state.alert_count < MAX_HEALTH_ALERTS:
            delay_needed = ESCALATION_DELAYS_SECONDS[state.alert_count - 1]
            time_since_last_alert = now - (state.last_alert_ts or now)

            if time_since_last_alert >= delay_needed:
                state.alert_count += 1
                state.last_alert_ts = now

                msg = self.format_offline_alert(
                    camera=camera,
                    current_fps=current_fps,
                    expected_fps=expected_fps,
                    alert_count=state.alert_count,
                    error_detail=state.last_error_detail,
                    timestamp_str=datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                )
                return HealthAlert(
                    camera=camera,
                    alert_type="offline",
                    alert_count=state.alert_count,
                    message=msg,
                )

        return None

    def format_offline_alert(
        self,
        camera: str,
        current_fps: float,
        expected_fps: float,
        alert_count: int,
        error_detail: str | None = None,
        timestamp_str: str | None = None,
    ) -> str:
        """Format an HTML offline alert message for Telegram."""
        if timestamp_str is None:
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if alert_count == 1:
            next_interval_text = "Next in 5 min if unresolved"
        elif alert_count == 2:
            next_interval_text = "Next in 60 min if unresolved"
        elif alert_count == 3:
            next_interval_text = "Next in 12 hours if unresolved"
        elif alert_count == 4:
            next_interval_text = "Next in 24 hours if unresolved"
        else:
            next_interval_text = "Final reminder until recovery"

        details_block = ""
        if error_detail:
            details_block = f"\n<b>Details:</b>\n<code>{html.escape(error_detail)}</code>"
        else:
            details_block = f"\n<b>Details:</b>\n<code>No frames received (camera_fps: {current_fps:.1f})</code>"

        expected_text = f" (Expected: {expected_fps:.1f} fps)" if expected_fps > 0 else ""

        return (
            f"⚠️ <b>Camera Alert: {html.escape(camera)} is OFFLINE</b>\n\n"
            f"<b>Camera:</b> <code>{html.escape(camera)}</code>\n"
            f"<b>Issue:</b> No frames received / Stream disconnected\n"
            f"<b>FPS:</b> {current_fps:.1f} fps{expected_text}\n"
            f"<b>Alert:</b> {alert_count} of {MAX_HEALTH_ALERTS} ({next_interval_text})"
            f"{details_block}\n"
            f"<b>Time:</b> {timestamp_str}"
        )

    def format_recovery_alert(
        self,
        camera: str,
        current_fps: float,
        downtime_seconds: float,
        timestamp_str: str | None = None,
    ) -> str:
        """Format an HTML recovery alert message for Telegram."""
        if timestamp_str is None:
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        downtime_str = format_downtime(downtime_seconds)

        return (
            f"✅ <b>Camera Restored: {html.escape(camera)} is BACK ONLINE</b>\n\n"
            f"<b>Camera:</b> <code>{html.escape(camera)}</code>\n"
            f"<b>Status:</b> Stream reconnected & receiving frames\n"
            f"<b>Current FPS:</b> {current_fps:.1f} fps\n"
            f"<b>Downtime:</b> {downtime_str}\n"
            f"<b>Time:</b> {timestamp_str}"
        )

    def evaluate_stats(
        self,
        stats_data: dict[str, Any],
        now: float | None = None,
    ) -> list[HealthAlert]:
        """Evaluate /api/stats payload and return any alerts due."""
        if now is None:
            now = time.time()

        # `.get(key, default)` only covers a *missing* key — Frigate can
        # plausibly return a key present with value `None` (e.g. during its
        # own startup/restart), which `.get` would pass straight through.
        # `or {}` normalizes both "missing" and "present but None" the same way.
        service = stats_data.get("service") or {}
        # `or 0` also covers "key present but None" — same rationale as above.
        uptime = service.get("uptime") or 0
        # Skip checks during Frigate startup grace period (< 60s)
        if uptime < 60:
            return []

        cameras_stats = stats_data.get("cameras") or {}
        alerts: list[HealthAlert] = []

        for camera, data in cameras_stats.items():
            if not self.should_monitor(camera):
                continue

            data = data or {}
            current_fps = float(data.get("camera_fps") or 0.0)
            # `is None` (not `or`) here: unlike camera_fps/uptime, the
            # default (5.0) doesn't equal the legit-zero value, so `or`
            # would wrongly mask a real `expected_fps: 0`.
            raw_expected_fps = data.get("expected_fps")
            expected_fps = (
                float(raw_expected_fps) if raw_expected_fps is not None else 5.0
            )
            connection_quality = str(data.get("connection_quality") or "").lower()

            # Flagged as failing if fps < 0.1 or connection unusable
            is_failing = (current_fps < 0.1) or (connection_quality == "unusable")

            alert = self.update_camera(
                camera=camera,
                is_failing=is_failing,
                current_fps=current_fps,
                expected_fps=expected_fps,
                now=now,
            )
            if alert is not None:
                alerts.append(alert)

        return alerts


async def fetch_log_error_detail(
    client: httpx.AsyncClient,
    frigate_url: str,
    camera: str,
    auth: tuple[str, str] | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Fetch recent error log lines for the camera from /api/logs/frigate.

    Gracefully returns None if unauthorized (401/403), unreachable, or no error found.
    """
    url = f"{frigate_url.rstrip('/')}/api/logs/frigate"
    params = {"start": -50}
    try:
        resp = await client.get(url, params=params, auth=auth, timeout=timeout)
        if resp.status_code != 200:
            return None

        data = resp.json()
        lines = data.get("lines", [])
        if not lines:
            return None

        cam_lower = camera.lower()

        for line in reversed(lines):
            line_str = str(line)
            line_lower = line_str.lower()
            if cam_lower in line_lower:
                if any(kw in line_lower for kw in [
                    "error",
                    "timed out",
                    "process is not running",
                    "unable to read frames",
                    "terminating",
                    "exiting",
                ]):
                    if "  " in line_str:
                        line_str = line_str.split("  ", 1)[1].strip()
                    return line_str
    except Exception as exc:
        logger.debug("Failed to fetch logs for camera %s: %s", camera, exc)
        return None

    return None



# ─────────────── Frigate recording cache (/tmp/cache) fill alert ───────────────

# Frigate's recording maintainer moves ~10s segments out of this tmpfs. When it
# stalls, the cache fills to 100%, ffmpeg hits "No space left on device" and no
# recordings are written (clip.mp4 comes back 0 bytes) until Frigate restarts.
CACHE_STORAGE_PATH = "/tmp/cache"


def _is_number(value: Any) -> bool:
    # bool is an int subclass; a boolean is never a real MiB reading.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class CacheStorageMonitor(CameraHealthMonitor):
    """Reuses the camera debounce/escalation/recovery state machine for the
    /tmp/cache fill level, keyed by CACHE_STORAGE_PATH.

    A separate instance (not a pseudo-camera on the camera monitor) keeps it out
    of HEALTH_MONITOR_CAMERAS filtering and the per-camera /status listing. Only
    the message wording differs, so the two formatters `update_camera` calls are
    overridden to render storage text from the last observed reading.
    """

    def __init__(self, monitored_cameras: list[str] | None = None, debounce_seconds: float = 60.0) -> None:
        super().__init__(monitored_cameras=monitored_cameras, debounce_seconds=debounce_seconds)
        # Last reading (MiB / percent), None when unavailable. Read by /status.
        self.last_pct: float | None = None
        self.last_used: float | None = None
        self.last_total: float | None = None

    def evaluate(
        self,
        stats_data: dict[str, Any] | None,
        threshold_pct: float,
        now: float | None = None,
    ) -> HealthAlert | None:
        """Evaluate the /tmp/cache entry of an /api/stats payload."""
        now = time.time() if now is None else now

        # Same `or {}` rationale as evaluate_stats: keys can be present-but-None.
        # isinstance guards: Frigate writes `{}` when disk_usage fails, and a
        # malformed payload must be a quiet no-op, never break the camera check.
        service = (stats_data or {}).get("service") or {}
        storage = service.get("storage") if isinstance(service, dict) else None
        entry = storage.get(CACHE_STORAGE_PATH) if isinstance(storage, dict) else None
        used = entry.get("used") if isinstance(entry, dict) else None
        total = entry.get("total") if isinstance(entry, dict) else None

        if not (_is_number(used) and _is_number(total) and total > 0):
            # Unavailable reading: leave the state machine untouched so a
            # pending debounce/escalation isn't reset by one bad tick.
            self.last_pct = self.last_used = self.last_total = None
            return None

        self.last_used = float(used)
        self.last_total = float(total)
        self.last_pct = self.last_used / self.last_total * 100

        # Same startup grace as camera checks: /status still gets the reading.
        uptime = service.get("uptime") or 0
        if uptime < 60:
            return None

        return self.update_camera(CACHE_STORAGE_PATH, self.last_pct >= threshold_pct, now=now)

    def format_offline_alert(
        self,
        camera: str,
        current_fps: float,
        expected_fps: float,
        alert_count: int,
        error_detail: str | None = None,
        timestamp_str: str | None = None,
    ) -> str:
        """Storage wording for the offline alert (fps args are ignored)."""
        if timestamp_str is None:
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"🚨 <b>Frigate recording cache almost full</b>\n\n"
            f"<b>Cache:</b> {html.escape(camera)} {self.last_pct or 0:.0f}% "
            f"({self.last_used or 0:.0f}/{self.last_total or 0:.0f} MiB)\n"
            f"Recordings are likely not being saved (clips will be missing). "
            f"Restart Frigate: <code>docker compose restart frigate</code>\n"
            f"<b>Alert:</b> {alert_count}/{MAX_HEALTH_ALERTS}\n"
            f"<b>Time:</b> {timestamp_str}"
        )

    def format_recovery_alert(
        self,
        camera: str,
        current_fps: float,
        downtime_seconds: float,
        timestamp_str: str | None = None,
    ) -> str:
        """Storage wording for the recovery alert (fps arg is ignored)."""
        if timestamp_str is None:
            timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"✅ <b>Frigate recording cache recovered</b>\n\n"
            f"<b>Cache:</b> {html.escape(camera)} {self.last_pct or 0:.0f}%\n"
            f"<b>Duration:</b> {format_downtime(downtime_seconds)}\n"
            f"<b>Time:</b> {timestamp_str}"
        )
