"""Unit tests for Camera Health Monitor data models, state machine, and alerting."""

import pytest
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
