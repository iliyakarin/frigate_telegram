"""Unit tests for Camera Health Monitor data models, state machine, and alerting."""

import pytest
import camera_health
from camera_health import (
    CameraHealthMonitor,
    CameraHealthState,
    HealthAlert,
    format_downtime,
    parse_monitored_cameras,
)


def test_parse_monitored_cameras():
    # None or empty returns None (meaning all cameras)
    assert parse_monitored_cameras(None) is None
    assert parse_monitored_cameras("") is None
    assert parse_monitored_cameras("   ") is None

    # Comma-separated list
    assert parse_monitored_cameras("front_door, back_yard") == ["front_door", "back_yard"]
    assert parse_monitored_cameras("front_door") == ["front_door"]

    # JSON list
    assert parse_monitored_cameras('["front_door", "back_yard"]') == ["front_door", "back_yard"]

    # Invalid JSON falls back to comma separation
    assert parse_monitored_cameras("[front_door, back_yard") == ["[front_door", "back_yard"]


def test_format_downtime():
    assert format_downtime(45) == "45s"
    assert format_downtime(125) == "2m 5s"
    assert format_downtime(3665) == "1h 1m 5s"
    assert format_downtime(86400 + 3600) == "1d 1h"


def test_healthy_camera_no_alert():
    monitor = CameraHealthMonitor(debounce_seconds=60)
    alert = monitor.update_camera("cam1", is_failing=False, current_fps=5.0, expected_fps=5.0, now=1000.0)
    assert alert is None
    assert monitor.states["cam1"].is_offline is False
    assert monitor.states["cam1"].notified_offline is False


def test_debounce_window():
    monitor = CameraHealthMonitor(debounce_seconds=60)

    # Initial failure detected at t=1000
    alert = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=1000.0)
    assert alert is None  # Pending debounce
    state = monitor.states["cam1"]
    assert state.first_failure_ts == 1000.0
    assert state.alert_count == 0

    # Still failing at t=1030 (30s elapsed < 60s debounce)
    alert = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=1030.0)
    assert alert is None

    # Recovers at t=1045 before debounce elapsed
    alert = monitor.update_camera("cam1", is_failing=False, current_fps=5.0, expected_fps=5.0, now=1045.0)
    assert alert is None
    assert monitor.states["cam1"].first_failure_ts is None
    assert monitor.states["cam1"].alert_count == 0

    # Fails again at t=1100
    alert = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=1100.0)
    assert alert is None

    # Debounce reached at t=1160 (60s elapsed)
    alert = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=1160.0)
    assert alert is not None
    assert alert.alert_type == "offline"
    assert alert.alert_count == 1
    assert alert.camera == "cam1"
    assert monitor.states["cam1"].notified_offline is True
    assert monitor.states["cam1"].last_alert_ts == 1160.0


def test_repeat_escalation_schedule():
    monitor = CameraHealthMonitor(debounce_seconds=60)
    t = 1000.0

    # Trigger initial alert (Alert 1) at t=1060
    monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t)
    alert1 = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t + 60.0)
    assert alert1 is not None
    assert alert1.alert_count == 1
    last_alert_t = t + 60.0

    # 4 minutes later (+240s): should NOT send repeat yet (< 300s / 5m)
    assert monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 240.0) is None

    # 5 minutes later (+300s): Alert 2
    alert2 = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 300.0)
    assert alert2 is not None
    assert alert2.alert_count == 2
    last_alert_t += 300.0

    # 30 minutes later: should NOT send repeat yet (< 3600s / 60m)
    assert monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 1800.0) is None

    # 60 minutes later (+3600s): Alert 3
    alert3 = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 3600.0)
    assert alert3 is not None
    assert alert3.alert_count == 3
    last_alert_t += 3600.0

    # 12 hours later (+43200s): Alert 4
    alert4 = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 43200.0)
    assert alert4 is not None
    assert alert4.alert_count == 4
    last_alert_t += 43200.0

    # 24 hours later (+86400s): Alert 5
    alert5 = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 86400.0)
    assert alert5 is not None
    assert alert5.alert_count == 5
    last_alert_t += 86400.0

    # Another 24 hours later: Silenced! Max 5 alerts sent
    alert6 = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=last_alert_t + 86400.0)
    assert alert6 is None


def test_recovery_notification_and_state_reset():
    monitor = CameraHealthMonitor(debounce_seconds=60)
    t = 1000.0

    # Fail and alert
    monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t)
    monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t + 60.0)
    # 5m repeat
    monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t + 360.0)

    # Recover at t=1600 (downtime = 600s)
    recovery_alert = monitor.update_camera("cam1", is_failing=False, current_fps=5.0, expected_fps=5.0, now=t + 600.0)
    assert recovery_alert is not None
    assert recovery_alert.alert_type == "recovery"
    assert "BACK ONLINE" in recovery_alert.message
    assert "10m" in recovery_alert.message

    # Verify state is completely reset
    state = monitor.states["cam1"]
    assert state.is_offline is False
    assert state.notified_offline is False
    assert state.alert_count == 0
    assert state.first_failure_ts is None
    assert state.last_alert_ts is None

    # Next failure requires full debounce again
    assert monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t + 1000.0) is None
    new_alert = monitor.update_camera("cam1", is_failing=True, current_fps=0.0, expected_fps=5.0, now=t + 1060.0)
    assert new_alert is not None
    assert new_alert.alert_count == 1  # Restarts at 1


def test_message_formatting():
    monitor = CameraHealthMonitor()
    offline_msg = monitor.format_offline_alert(
        camera="RightBackyard",
        current_fps=0.0,
        expected_fps=5.0,
        alert_count=1,
        error_detail="[in#0/rtsp] Error during demuxing: Connection timed out",
        timestamp_str="2026-09-18 16:55:00",
    )
    assert "RightBackyard is OFFLINE" in offline_msg
    assert "1 of 5" in offline_msg
    assert "Connection timed out" in offline_msg
    assert "Next in 5 min" in offline_msg

    recovery_msg = monitor.format_recovery_alert(
        camera="RightBackyard",
        current_fps=5.0,
        downtime_seconds=370,
        timestamp_str="2026-09-18 17:01:10",
    )
    assert "RightBackyard is BACK ONLINE" in recovery_msg
    assert "6m 10s" in recovery_msg
    assert "5.0 fps" in recovery_msg


def test_startup_grace_period_skips_alerts():
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {
        "service": {"uptime": 30},  # < 60s
        "cameras": {
            "cam1": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"}
        }
    }
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    assert alerts == []
    assert "cam1" not in monitor.states  # No state tracked during startup grace


def test_evaluate_stats_flags_unusable_and_zero_fps():
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats_failing = {
        "service": {"uptime": 120},
        "cameras": {
            "cam1": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"},
            "cam2": {"camera_fps": 5.0, "expected_fps": 5.0, "connection_quality": "excellent"},
        }
    }

    # At t=1000: first failure detected, pending debounce
    assert monitor.evaluate_stats(stats_failing, now=1000.0) == []
    # At t=1060: debounce reached, Alert 1 for cam1
    alerts = monitor.evaluate_stats(stats_failing, now=1060.0)
    assert len(alerts) == 1
    assert alerts[0].camera == "cam1"
    assert alerts[0].alert_type == "offline"

    # At t=1100: cam1 recovers
    stats_recovered = {
        "service": {"uptime": 160},
        "cameras": {
            "cam1": {"camera_fps": 5.0, "expected_fps": 5.0, "connection_quality": "excellent"},
            "cam2": {"camera_fps": 5.0, "expected_fps": 5.0, "connection_quality": "excellent"},
        }
    }
    recovery_alerts = monitor.evaluate_stats(stats_recovered, now=1100.0)
    assert len(recovery_alerts) == 1
    assert recovery_alerts[0].camera == "cam1"
    assert recovery_alerts[0].alert_type == "recovery"


def test_evaluate_stats_filters_cameras():
    # Only monitor cam1
    monitor = CameraHealthMonitor(monitored_cameras=["cam1"], debounce_seconds=60)
    stats = {
        "service": {"uptime": 120},
        "cameras": {
            "cam1": {"camera_fps": 5.0, "expected_fps": 5.0, "connection_quality": "excellent"},
            "cam2": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"},
        }
    }
    # cam2 is failing, but should be ignored because monitored_cameras=["cam1"]
    assert monitor.evaluate_stats(stats, now=1000.0) == []
    assert monitor.evaluate_stats(stats, now=1060.0) == []
    assert "cam2" not in monitor.states


def test_evaluate_stats_handles_null_service_without_raising():
    """Regression: Frigate can plausibly return `"service": null` during its
    own startup/restart. evaluate_stats must not raise AttributeError on a
    key present with value None (dict.get's default only covers a *missing*
    key, not a null one)."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {"service": None, "cameras": {}}
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    assert alerts == []


def test_evaluate_stats_handles_null_cameras_without_raising():
    """Regression: `"cameras": null` (key present but null) must not raise
    either, and must be treated as "no cameras to evaluate"."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {"service": {"uptime": 120}, "cameras": None}
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    assert alerts == []


def test_evaluate_stats_handles_null_camera_entry_without_raising():
    """Regression: a single camera's stats sub-dict being null must not
    raise, and must not stop the remaining cameras from being evaluated."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {
        "service": {"uptime": 120},
        "cameras": {
            "cam1": None,
            "cam2": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"},
        },
    }
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    # cam1's null entry doesn't raise; cam2 is still evaluated normally
    # (first pass is pending debounce, so no alert is due yet either way).
    assert alerts == []
    assert "cam2" in monitor.states
    assert monitor.states["cam2"].current_fps == 0.0


def test_evaluate_stats_handles_null_uptime_without_raising():
    """Regression: `"uptime": null` (key present but null, container itself
    non-null) must not raise on the `uptime < 60` comparison."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {"service": {"uptime": None}, "cameras": {}}
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    assert alerts == []


def test_evaluate_stats_handles_null_camera_fps_without_raising():
    """Regression: a camera's `camera_fps`/`expected_fps` present but null
    (plausible mid-reconnect) must not raise on `float(None)`."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {
        "service": {"uptime": 120},
        "cameras": {
            "cam1": {"camera_fps": None, "expected_fps": None, "connection_quality": "unusable"},
        },
    }
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    # First pass is pending debounce, so no alert is due yet either way.
    assert alerts == []
    assert monitor.states["cam1"].current_fps == 0.0


def test_evaluate_stats_preserves_legit_zero_expected_fps():
    """Regression: `expected_fps: 0` is a legitimate value (e.g. detection
    paused), not a missing one. `or 5.0` would wrongly mask it as 5.0 -
    only an explicit `is None` check preserves the real 0."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {
        "service": {"uptime": 120},
        "cameras": {
            "cam1": {"camera_fps": 0.0, "expected_fps": 0, "connection_quality": "unusable"},
        },
    }
    monitor.evaluate_stats(stats, now=1000.0)
    assert monitor.states["cam1"].expected_fps == 0.0


def test_evaluate_stats_handles_null_connection_quality_without_raising():
    """Regression: `"connection_quality": null` must not raise, and must
    not spuriously match the "unusable" failing condition."""
    monitor = CameraHealthMonitor(debounce_seconds=60)
    stats = {
        "service": {"uptime": 120},
        "cameras": {
            "cam1": {"camera_fps": 5.0, "expected_fps": 5.0, "connection_quality": None},
        },
    }
    alerts = monitor.evaluate_stats(stats, now=1000.0)
    assert alerts == []


@pytest.mark.asyncio
async def test_fetch_log_error_detail_success():
    import httpx
    from camera_health import fetch_log_error_detail

    sample_logs = {
        "totalLines": 5,
        "lines": [
            "2026-09-18 16:54:50  [INFO] [watchdog.Garage] Checking camera status",
            "2026-09-18 16:54:58  [ERROR] [ffmpeg.RightBackyard.record] [in#0/rtsp @ 0x5c1c27993e80] Error during demuxing: Connection timed out",
            "2026-09-18 16:55:00  [ERROR] [frigate.video] RightBackyard: Unable to read frames from ffmpeg process.",
        ]
    }

    async def mock_handler(request: httpx.Request):
        assert request.url.path == "/api/logs/frigate"
        return httpx.Response(200, json=sample_logs)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        detail = await fetch_log_error_detail(client, "http://frigate:5000", "RightBackyard")
        assert detail is not None
        # Returns the latest relevant error
        assert "Unable to read frames from ffmpeg process" in detail or "Connection timed out" in detail


@pytest.mark.asyncio
async def test_fetch_log_error_detail_fallback():
    import httpx
    from camera_health import fetch_log_error_detail

    # Test 403 Forbidden (no admin permissions)
    async def mock_forbidden(request: httpx.Request):
        return httpx.Response(403, json={"message": "Forbidden"})

    transport = httpx.MockTransport(mock_forbidden)
    async with httpx.AsyncClient(transport=transport) as client:
        detail = await fetch_log_error_detail(client, "http://frigate:5000", "RightBackyard")
        assert detail is None

    # Test Network error
    async def mock_error(request: httpx.Request):
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(mock_error)
    async with httpx.AsyncClient(transport=transport) as client:
        detail = await fetch_log_error_detail(client, "http://frigate:5000", "RightBackyard")
        assert detail is None


# ─────────────── Frigate /tmp/cache storage alert (CacheStorageMonitor) ───────────────
# CacheStorageMonitor is referenced lazily (inside each test) so a missing
# class fails only these tests, not collection of the whole module.


CACHE_KEY = "/tmp/cache"


def _cache_monitor():
    return camera_health.CacheStorageMonitor(monitored_cameras=None, debounce_seconds=60)


def _cache_stats(used, total=2048.0, uptime=120):
    """/api/stats payload shaped like Frigate 0.16.4 (MiB floats)."""
    return {
        "service": {
            "uptime": uptime,
            "storage": {
                "/media/frigate/recordings": {"total": 1006828.1, "used": 210071.4, "free": 745540.7, "mount_type": "ext4"},
                CACHE_KEY: {"total": total, "used": used, "free": (total or 0) - (used or 0), "mount_type": "tmpfs"},
            },
        },
        "cameras": {},
    }


def test_cache_storage_path_constant():
    assert camera_health.CACHE_STORAGE_PATH == "/tmp/cache"


def test_cache_below_threshold_no_alert():
    monitor = _cache_monitor()
    # Prod healthy numbers: 2048 MiB tmpfs, 32.1 MiB used (~1.6%).
    assert monitor.evaluate(_cache_stats(32.1), 85, now=1000.0) is None
    assert monitor.evaluate(_cache_stats(32.1), 85, now=1200.0) is None
    assert monitor.last_pct == pytest.approx(32.1 / 2048.0 * 100)


def test_cache_last_fields_track_observed_usage():
    monitor = _cache_monitor()
    assert monitor.last_pct is None
    monitor.evaluate(_cache_stats(1000.0, total=2048.0), 85, now=1000.0)
    assert monitor.last_pct == pytest.approx(1000.0 / 2048.0 * 100)
    assert monitor.last_used == pytest.approx(1000.0)
    assert monitor.last_total == pytest.approx(2048.0)


def test_cache_over_threshold_debounced_then_alerts_with_storage_wording():
    monitor = _cache_monitor()
    full = _cache_stats(2000.0)

    assert monitor.evaluate(full, 85, now=1000.0) is None  # debounce starts
    assert monitor.evaluate(full, 85, now=1030.0) is None  # 30s < 60s

    alert = monitor.evaluate(full, 85, now=1061.0)
    assert alert is not None
    assert alert.alert_type == "offline"
    assert alert.alert_count == 1
    msg = alert.message
    assert "cache" in msg.lower()
    assert "/tmp/cache" in msg
    assert "fps" not in msg.lower()
    assert "98%" in msg  # 2000/2048 = 97.66% → rendered {pct:.0f}
    assert "2000" in msg and "2048" in msg  # used/total MiB
    assert "docker compose restart frigate" in msg
    assert "1/5" in msg
    assert "Next in" not in msg  # adjudication #10: no "Next in" line
    assert "<b>Time:</b>" in msg


def test_cache_threshold_boundary_is_inclusive():
    monitor = _cache_monitor()
    at_threshold = _cache_stats(850.0, total=1000.0)  # exactly 85%
    monitor.evaluate(at_threshold, 85, now=1000.0)
    alert = monitor.evaluate(at_threshold, 85, now=1060.0)
    assert alert is not None
    assert alert.alert_type == "offline"


def test_cache_escalation_follows_camera_schedule():
    monitor = _cache_monitor()
    full = _cache_stats(2000.0)

    monitor.evaluate(full, 85, now=1000.0)
    alert = monitor.evaluate(full, 85, now=1060.0)
    assert alert.alert_count == 1

    t = 1060.0
    for expected_count, delay in zip(range(2, 6), camera_health.ESCALATION_DELAYS_SECONDS):
        assert monitor.evaluate(full, 85, now=t + delay - 1) is None
        t = t + delay
        alert = monitor.evaluate(full, 85, now=t)
        assert alert is not None
        assert alert.alert_count == expected_count
        assert f"{expected_count}/5" in alert.message
        assert "/tmp/cache" in alert.message

    # After 5 alerts it stays silent until recovery.
    assert monitor.evaluate(full, 85, now=t + 10 * 86400) is None


def test_cache_recovery_after_alert_sends_recovery_and_resets_state():
    monitor = _cache_monitor()
    full = _cache_stats(2000.0)
    monitor.evaluate(full, 85, now=1000.0)
    assert monitor.evaluate(full, 85, now=1060.0) is not None

    recovery = monitor.evaluate(_cache_stats(32.1), 85, now=1125.0)
    assert recovery is not None
    assert recovery.alert_type == "recovery"
    msg = recovery.message
    assert "cache" in msg.lower()
    assert "recovered" in msg.lower()
    assert "fps" not in msg.lower()
    assert "2%" in msg  # 32.1/2048 = 1.57% → {pct:.0f}
    assert "2m 5s" in msg  # downtime from first failure at t=1000
    assert "<b>Time:</b>" in msg

    state = monitor.states[CACHE_KEY]
    assert state.notified_offline is False
    assert state.alert_count == 0
    assert state.first_failure_ts is None

    # Next healthy tick is quiet.
    assert monitor.evaluate(_cache_stats(32.1), 85, now=1200.0) is None


def test_cache_recovery_before_first_alert_is_silent():
    monitor = _cache_monitor()
    assert monitor.evaluate(_cache_stats(2000.0), 85, now=1000.0) is None
    assert monitor.evaluate(_cache_stats(32.1), 85, now=1030.0) is None
    assert monitor.states[CACHE_KEY].first_failure_ts is None


@pytest.mark.parametrize("uptime", [30, None, "abc"], ids=["uptime_below_60", "uptime_missing", "uptime_non_numeric"])
def test_cache_uptime_grace_skips_state_but_records_pct(uptime):
    monitor = _cache_monitor()
    stats = _cache_stats(2000.0, uptime=uptime)
    if uptime is None:
        del stats["service"]["uptime"]
    assert monitor.evaluate(stats, 85, now=1000.0) is None
    assert monitor.evaluate(stats, 85, now=2000.0) is None
    assert CACHE_KEY not in monitor.states
    # /status still gets a fresh reading during the grace window.
    assert monitor.last_pct == pytest.approx(2000.0 / 2048.0 * 100)


def _drop(path):
    """Build a malformed payload by mutating a fresh full-cache payload."""
    def build():
        stats = _cache_stats(2000.0)
        path(stats)
        return stats
    return build


@pytest.mark.parametrize(
    "build_stats",
    [
        lambda: None,
        _drop(lambda s: s.__setitem__("service", None)),
        _drop(lambda s: s["service"].pop("storage")),
        _drop(lambda s: s["service"].__setitem__("storage", None)),
        _drop(lambda s: s["service"]["storage"].pop(CACHE_KEY)),
        _drop(lambda s: s["service"]["storage"].__setitem__(CACHE_KEY, {})),
        _drop(lambda s: s["service"]["storage"].__setitem__(CACHE_KEY, None)),
        _drop(lambda s: s["service"]["storage"][CACHE_KEY].__setitem__("total", 0)),
        _drop(lambda s: s["service"]["storage"][CACHE_KEY].__setitem__("total", None)),
        _drop(lambda s: s["service"]["storage"][CACHE_KEY].__setitem__("used", None)),
        _drop(lambda s: s["service"]["storage"][CACHE_KEY].__setitem__("used", "lots")),
    ],
    ids=[
        "stats_none",
        "service_none",
        "storage_missing",
        "storage_none",
        "cache_entry_missing",
        "cache_entry_empty",
        "cache_entry_none",
        "total_zero",
        "total_none",
        "used_none",
        "used_non_numeric",
    ],
)
def test_cache_unavailable_payload_is_quiet_noop(build_stats):
    monitor = _cache_monitor()
    for now in (1000.0, 1100.0, 1200.0):
        assert monitor.evaluate(build_stats(), 85, now=now) is None
    assert CACHE_KEY not in monitor.states
    assert monitor.last_pct is None


def test_cache_unavailable_tick_does_not_reset_pending_debounce():
    monitor = _cache_monitor()
    full = _cache_stats(2000.0)
    monitor.evaluate(full, 85, now=1000.0)
    assert monitor.evaluate(None, 85, now=1030.0) is None  # untouched state
    alert = monitor.evaluate(full, 85, now=1061.0)
    assert alert is not None and alert.alert_type == "offline"


def test_cache_threshold_parameter_is_respected():
    stats = _cache_stats(1100.0)  # 53.7%

    strict = _cache_monitor()
    strict.evaluate(stats, 85, now=1000.0)
    assert strict.evaluate(stats, 85, now=1100.0) is None

    lax = _cache_monitor()
    lax.evaluate(stats, 50, now=1000.0)
    alert = lax.evaluate(stats, 50, now=1100.0)
    assert alert is not None
    assert alert.alert_type == "offline"
    assert "54%" in alert.message

