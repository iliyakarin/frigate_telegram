# Camera Health Alerts Design Specification

## Overview
Adds automated health monitoring and alerting to the `frigate-telegram` bot. The bot continuously monitors cameras for frame loss, stream disconnections, and recording failures using Frigate's `/api/stats` and `/api/logs/frigate` endpoints, delivering debounced alerts and repeated escalation notices (at 5m, 60m, 12h, and 24h) until the camera recovers, followed by a recovery notification.

---

## Configuration

### Environment Variables
- **`HEALTH_MONITOR_CAMERAS`** (optional):
  - Comma-separated list (e.g. `front_door,back_camera`) or JSON array (e.g. `["front_door", "back_camera"]`).
  - When unset or empty, monitors **all cameras** returned by Frigate.
- **`HEALTH_DEBOUNCE_SECONDS`** (optional, default: `60`):
  - Number of seconds a camera must continually fail before the first alert is dispatched.
- **Escalation Repeat Schedule** (seconds after prior alert):
  - Alert #1: Initial alert (fired when offline >= `HEALTH_DEBOUNCE_SECONDS`)
  - Alert #2: `+300`s (5 minutes)
  - Alert #3: `+3600`s (60 minutes)
  - Alert #4: `+43200`s (12 hours)
  - Alert #5: `+86400`s (24 hours)
  - Silenced after Alert #5 until camera recovers and drops again.

---

## Architecture & Data Flow

### 1. New Module: `camera_health.py`
Contains:
- `CameraHealthState`:
  - `camera: str`
  - `first_failure_ts: float | None`
  - `last_alert_ts: float | None`
  - `alert_count: int` (0 = healthy / no alerts sent, 1..5 = alerts dispatched)
  - `last_error_detail: str | None`
  - `notified_offline: bool`
- `CameraHealthMonitor`:
  - Maintains `states: dict[str, CameraHealthState]`.
  - `evaluate_stats(stats_data: dict, now: float) -> tuple[list[HealthAlert], list[HealthAlert]]`:
    - Checks Frigate uptime: if `uptime < 60`, skips checks to avoid startup false positives.
    - Inspects `stats_data["cameras"]`. A camera is flagged as failing if `camera_fps < 0.1` or `connection_quality == "unusable"`.
    - Evaluates debounce timing, escalation schedule, and recovery transitions.
    - Returns `(alerts_to_send, recoveries_to_send)`.
  - `async fetch_log_error_detail(http_client: httpx.AsyncClient, camera: str) -> str | None`:
    - Queries `GET {FRIGATE_URL}/api/logs/frigate?start=-50`.
    - Searches log lines for matching tags/messages (`ffmpeg.<camera>`, `watchdog.<camera>`, `frigate.video: <camera>`).
    - Extracts the most recent error line (e.g., demuxing timeout, thread exit).
    - Gracefully falls back to stats summary if unauthorized (401/403), timed out, or no matching line found.

### 2. Integration in `main.py`
- In `_polling_tick`:
  - Query `GET {FRIGATE_URL}/api/stats`.
  - Pass stats into `CameraHealthMonitor`.
  - For each alert due:
    - Enrich with log snippet via `fetch_log_error_detail`.
    - Send HTML formatted Telegram alert to `TELEGRAM_CHAT_ID`.
  - For each recovery due:
    - Send HTML formatted Telegram recovery message to `TELEGRAM_CHAT_ID`.
- In `/status` and `/cameras`:
  - Query current camera health states and display visual status indicators (🟢 / 🔴) alongside FPS.

---

## Message Templates

### Offline Alert Template
```html
⚠️ <b>Camera Alert: {camera} is OFFLINE</b>

<b>Camera:</b> <code>{camera}</code>
<b>Issue:</b> No frames received / Stream disconnected
<b>FPS:</b> {current_fps} fps (Expected: {expected_fps} fps)
<b>Alert:</b> {alert_count} of 5 ({next_interval_text})
<b>Details:</b>
<code>{error_details}</code>
<b>Time:</b> {formatted_time}
```

### Recovery Alert Template
```html
✅ <b>Camera Restored: {camera} is BACK ONLINE</b>

<b>Camera:</b> <code>{camera}</code>
<b>Status:</b> Stream reconnected & receiving frames
<b>Current FPS:</b> {current_fps} fps
<b>Downtime:</b> {downtime_text}
<b>Time:</b> {formatted_time}
```

---

## Testing Strategy

All new functionality will follow strict TDD in `test_camera_health.py` and `test_main.py`:
1. `test_healthy_cameras_no_alerts`: Normal FPS generates no alerts.
2. `test_startup_grace_period`: Uptime < 60 suppresses alerts.
3. `test_debounce_transient_failure`: A camera failing for < 60s and recovering produces no alert.
4. `test_initial_alert_after_debounce`: A camera failing for >= 60s produces initial alert.
5. `test_escalation_intervals`: Verifies exact repeats after 5m, 60m, 12h, and 24h, and suppression thereafter.
6. `test_recovery_notification_and_reset`: Verifies recovery alert is sent with correct downtime, and state is reset.
7. `test_log_error_extraction`: Correctly parses matching ffmpeg/watchdog error messages from log lines.
8. `test_log_error_fallback`: Gracefully falls back to stats when logs API fails.
9. `test_health_monitor_cameras_filter`: Filters to specified cameras or defaults to all cameras.
10. `test_polling_tick_dispatches_health_alerts`: Full tick integration test with mock bot and HTTP client.
11. `test_status_and_cameras_commands_include_health`: Verifies bot commands render camera health indicators.
