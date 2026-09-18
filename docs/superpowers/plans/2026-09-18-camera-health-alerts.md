# Camera Health Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automated camera health monitoring to `frigate-telegram` that detects frame loss, disconnections, and recording failures via Frigate's `/api/stats` and `/api/logs/frigate`, debouncing initial alerts and escalating notifications after 5m, 60m, 12h, and 24h until recovery.

**Architecture:** A standalone `camera_health.py` module maintains per-camera health and escalation state machines. During `_polling_tick`, `/api/stats` is evaluated; when an alert is due, `/api/logs/frigate` is queried to extract specific demuxing/ffmpeg error logs (with graceful fallback to stats on failure), and formatted Telegram alerts and recovery notifications are dispatched.

**Tech Stack:** Python 3.10+, httpx (async HTTP), python-telegram-bot, pytest, pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-09-18-camera-health-alerts-design.md](file:///home/ikarin/Personal_projects/frigate_telegram/docs/superpowers/specs/2026-09-18-camera-health-alerts-design.md)

## Global Constraints
- Follow TDD (Red-Green-Refactor) for every task: tests written and verified failing before implementation.
- Preserve 100% test pass rate across existing test suites (`test_main.py`, `test_grouping.py`, `test_security.py`, `test_state.py`, `test_utils.py`).
- Keep `main.py` clean by delegating health domain logic to `camera_health.py` (KISS & SOLID).
- Support `HEALTH_MONITOR_CAMERAS` (comma-separated or JSON list, defaulting to all cameras when unset/empty).
- Escalation repeat schedule: Alert 1 (after 60s debounce), Alert 2 (+5m), Alert 3 (+60m), Alert 4 (+12h), Alert 5 (+24h), then silenced until recovery.

---

### Task 1: Core Health Data Models, State Machine & Formatter

**Files:**
- Create: `camera_health.py`
- Test: `test_camera_health.py`

**Interfaces:**
- Produces:
  - `parse_monitored_cameras(val: str | None) -> list[str] | None`
  - `CameraHealthState` dataclass
  - `HealthAlert` dataclass: `camera: str, alert_type: str, alert_count: int, message: str`
  - `CameraHealthMonitor` class:
    - `__init__(monitored_cameras: list[str] | None = None, debounce_seconds: float = 60.0)`
    - `update_camera(camera: str, is_failing: bool, current_fps: float, expected_fps: float, now: float) -> HealthAlert | None`
    - `format_offline_alert(camera: str, current_fps: float, expected_fps: float, alert_count: int, error_detail: str | None, now: float) -> str`
    - `format_recovery_alert(camera: str, current_fps: float, downtime_seconds: float, now: float) -> str`

- [ ] **Step 1: Write failing tests for state machine, debounce, repeats, and formatting**

Write `test_camera_health.py` testing:
- Parsing `HEALTH_MONITOR_CAMERAS` from JSON array or comma-separated string.
- Camera staying healthy produces no alert.
- Camera failing for < 60s (debounce) produces no alert.
- Camera failing for >= 60s produces Alert #1 (`alert_count=1`).
- Repeat schedule:
  - Alert 2 at t >= +300s (5m)
  - Alert 3 at t >= +3600s (60m)
  - Alert 4 at t >= +43200s (12h)
  - Alert 5 at t >= +86400s (24h)
  - No Alert 6 (silenced).
- Recovery notification generated on transition from offline to healthy with calculated downtime string.
- Resetting state on recovery so subsequent failure restarts at Alert #1.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_camera_health.py -v`
Expected: FAIL (ModuleNotFoundError or missing symbols)

- [ ] **Step 3: Implement `camera_health.py` data models and state machine**

Implement:
- `parse_monitored_cameras`
- `CameraHealthState`, `HealthAlert`
- `CameraHealthMonitor` with debounce and repeat interval logic
- Message formatting methods producing HTML

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_camera_health.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add camera_health.py test_camera_health.py
git commit -m "feat: implement camera health state machine and alerting logic"
```

---

### Task 2: Dual-Check Frigate API Evaluation & Error Log Parsing

**Files:**
- Modify: `camera_health.py`
- Test: `test_camera_health.py`

**Interfaces:**
- Consumes: `CameraHealthMonitor`, Frigate `/api/stats` and `/api/logs/frigate` JSON schemas
- Produces:
  - `CameraHealthMonitor.evaluate_stats(stats_data: dict, now: float) -> tuple[list[tuple[str, HealthAlert]], list[tuple[str, HealthAlert]]]`
  - `fetch_log_error_detail(client: httpx.AsyncClient, frigate_url: str, camera: str, auth: tuple | None = None, timeout: float = 5.0) -> str | None`

- [ ] **Step 1: Write failing tests for stats evaluation and log extraction**

Add tests to `test_camera_health.py`:
- `test_startup_grace_period_skips_alerts`: Uptime < 60s in stats skips failure checks.
- `test_evaluate_stats_flags_unusable_and_zero_fps`: Tests handling of `camera_fps < 0.1` and `connection_quality == "unusable"`.
- `test_evaluate_stats_filters_cameras`: Verifies only configured cameras are evaluated if a filter is set.
- `test_fetch_log_error_detail_success`: Mock `/api/logs/frigate` returning logs with `[in#0/rtsp] Error during demuxing: Connection timed out` for `RightBackyard` extracts the error text.
- `test_fetch_log_error_detail_fallback`: Mock 403 Forbidden or network timeout returns `None` cleanly without raising.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_camera_health.py -k "stats or log" -v`
Expected: FAIL

- [ ] **Step 3: Implement stats evaluation and log extraction in `camera_health.py`**

Implement:
- `evaluate_stats` using `camera_fps`, `expected_fps`, and `service.uptime`.
- `fetch_log_error_detail` parsing lines for `ffmpeg.{camera}`, `watchdog.{camera}`, and `frigate.video: {camera}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_camera_health.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add camera_health.py test_camera_health.py
git commit -m "feat: add stats evaluation and dual-check log error parsing to camera health monitor"
```

---

### Task 3: Integration into Polling Loop and Telegram Commands

**Files:**
- Modify: `main.py:40-60` (configuration loading), `main.py:930-970` (`cmd_status`, `cmd_cameras`), `main.py:1335-1430` (`_polling_tick`, `polling_loop`)
- Modify: `test_main.py`

**Interfaces:**
- Consumes: `CameraHealthMonitor`, `fetch_log_error_detail`
- Produces:
  - Configuration `HEALTH_MONITOR_CAMERAS` parsed into global `health_monitor`
  - Integrated health alerts in `_polling_tick`
  - Visual status indicators in `/status` and `/cameras`

- [ ] **Step 1: Write failing tests in `test_main.py`**

Add tests in `test_main.py`:
- `test_polling_tick_dispatches_camera_offline_alert`: Verifies `_polling_tick` calls stats, detects failure past debounce, fetches log details, and sends Telegram alert.
- `test_polling_tick_dispatches_camera_recovery_alert`: Verifies `_polling_tick` detects recovery and sends Telegram recovery message.
- `test_cmd_status_displays_camera_health`: Verifies `/status` output includes camera health status.
- `test_cmd_cameras_displays_camera_health`: Verifies `/cameras` output includes camera health indicators.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_main.py -k "camera_health or cmd_status or cmd_cameras" -v`
Expected: FAIL

- [ ] **Step 3: Implement integration in `main.py`**

- Initialize `health_monitor = CameraHealthMonitor(...)` with `HEALTH_MONITOR_CAMERAS`.
- In `_polling_tick`:
  - Fetch stats using `client.get(f"{FRIGATE_URL}/api/stats")`.
  - Evaluate health via `health_monitor.evaluate_stats(stats, now)`.
  - For each alert: fetch log detail asynchronously and call `bot.send_message(...)`.
  - For each recovery: call `bot.send_message(...)`.
- In `cmd_status` and `cmd_cameras`:
  - Display camera status indicators (🟢 / 🔴).

- [ ] **Step 4: Run full test suite to verify all pass**

Run: `python3 -m pytest`
Expected: All tests PASS (100% passing)

- [ ] **Step 5: Commit**

```bash
git add main.py test_main.py
git commit -m "feat: integrate camera health monitoring into polling loop and bot commands"
```

---

### Task 4: Documentation and Configuration Updates

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Update `.env.example` and `docker-compose.yml`**

Add `HEALTH_MONITOR_CAMERAS` with documentation comment explaining that leaving it blank monitors all cameras.

- [ ] **Step 2: Update `README.md`**

Document:
- Camera Health Alerting feature
- Dual-check detection mechanism (`/api/stats` and `/api/logs/frigate`)
- Debounce and escalation schedule (initial after 60s, +5m, +60m, +12h, +24h)
- Configuration options

- [ ] **Step 3: Verify git diff and run all tests**

Run: `python3 -m pytest`
Run: `git status`

- [ ] **Step 4: Commit**

```bash
git add README.md .env.example docker-compose.yml
git commit -m "docs: document camera health alerts and HEALTH_MONITOR_CAMERAS configuration"
```
