"""Camera health monitoring, debounce state machine, and alerting for Frigate cameras."""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

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
