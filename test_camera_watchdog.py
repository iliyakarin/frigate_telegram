"""Unit tests for Camera Watchdog (auto-reboot on sustained 0 FPS)."""

import httpx
import pytest

import camera_watchdog
from camera_watchdog import (
    ALLOWED_STREAM_SCHEMES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_REBOOT_COOLDOWN_SECONDS,
    DEFAULT_ZERO_FPS_THRESHOLD_SECONDS,
    FPS_FAILURE_THRESHOLD,
    FRIGATE_STARTUP_GRACE_SECONDS,
    LOOPBACK_HOSTS,
    CameraWatchdog,
    CameraWatchdogState,
    RebootAction,
    extract_go2rtc_stream_name,
    extract_host_from_stream_url,
    fetch_frigate_json,
    format_reboot_message,
    get_int_setting,
    reboot_camera_http,
    resolve_camera_ips,
    run_watchdog_tick,
    send_telegram_alert,
)


# ─────────────── 1. Constants & get_int_setting ───────────────


def test_constants_defaults():
    assert DEFAULT_ZERO_FPS_THRESHOLD_SECONDS == 300
    assert DEFAULT_REBOOT_COOLDOWN_SECONDS == 900
    assert DEFAULT_POLL_INTERVAL_SECONDS == 30
    assert FRIGATE_STARTUP_GRACE_SECONDS == 60
    assert FPS_FAILURE_THRESHOLD == 0.1
    assert "127.0.0.1" in LOOPBACK_HOSTS
    assert "localhost" in LOOPBACK_HOSTS
    assert "::1" in LOOPBACK_HOSTS
    assert "rtsp" in ALLOWED_STREAM_SCHEMES


def test_get_int_setting(monkeypatch, caplog):
    monkeypatch.delenv("TEST_WATCHDOG_INT", raising=False)
    assert get_int_setting("TEST_WATCHDOG_INT", 42) == 42

    monkeypatch.setenv("TEST_WATCHDOG_INT", "")
    assert get_int_setting("TEST_WATCHDOG_INT", 42) == 42

    monkeypatch.setenv("TEST_WATCHDOG_INT", "120")
    assert get_int_setting("TEST_WATCHDOG_INT", 42) == 120

    with caplog.at_level("WARNING", logger="frigate-watchdog"):
        monkeypatch.setenv("TEST_WATCHDOG_INT", "not-an-int")
        assert get_int_setting("TEST_WATCHDOG_INT", 42) == 42
    assert "TEST_WATCHDOG_INT" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING", logger="frigate-watchdog"):
        monkeypatch.setenv("TEST_WATCHDOG_INT", "0")
        assert get_int_setting("TEST_WATCHDOG_INT", 42, min_value=1) == 42
    assert "TEST_WATCHDOG_INT" in caplog.text


# ─────────────── 2. URL & IP Resolution ───────────────


def test_extract_host_from_stream_url_direct_and_templated():
    # Masked credentials from Frigate /api/config
    assert (
        extract_host_from_stream_url("rtsp://*:*@192.168.11.196:554/stream1")
        == "192.168.11.196"
    )
    # Template variables from go2rtc streams
    assert (
        extract_host_from_stream_url(
            "rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@192.168.11.200:554/cam/realmonitor?channel=1&subtype=0"
        )
        == "192.168.11.200"
    )
    # go2rtc ffmpeg: prefix and #fragment
    assert (
        extract_host_from_stream_url(
            "ffmpeg:rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@192.168.11.201:554/cam/realmonitor#video=copy#audio=opus"
        )
        == "192.168.11.201"
    )
    # Loopback restream URL
    assert (
        extract_host_from_stream_url("rtsp://127.0.0.1:8554/Garage_sub")
        == "127.0.0.1"
    )
    # Uppercase hostname normalized to lowercase
    assert (
        extract_host_from_stream_url("http://CamHost.Local:80/video")
        == "camhost.local"
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        None,
        123,
        "exec:/usr/bin/ffmpeg -i /dev/video0",
        "echo:hello",
        "ftp://192.168.11.50/file",
        "not_a_url_at_all",
        "rtsp:///missing_host",
        "ffmpeg:#only_fragment",
    ],
)
def test_extract_host_from_stream_url_invalid_or_disallowed(bad_url):
    assert extract_host_from_stream_url(bad_url) is None  # type: ignore[arg-type]


def test_extract_go2rtc_stream_name():
    assert extract_go2rtc_stream_name("rtsp://127.0.0.1:8554/Garage") == "Garage"
    assert (
        extract_go2rtc_stream_name(
            "ffmpeg:rtsp://127.0.0.1:8554/Garage_sub#video=copy"
        )
        == "Garage_sub"
    )
    assert (
        extract_go2rtc_stream_name("rtsp://localhost:8554/nested/stream_name/")
        == "stream_name"
    )
    assert extract_go2rtc_stream_name("rtsp://127.0.0.1:8554/") is None
    assert extract_go2rtc_stream_name("rtsp://127.0.0.1:8554") is None
    assert extract_go2rtc_stream_name("") is None
    assert extract_go2rtc_stream_name("not-a-url") is None


def test_resolve_camera_ips_direct_and_go2rtc_and_chained():
    config = {
        "go2rtc": {
            "streams": {
                # String form with template variables
                "Garage": "rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@192.168.11.200:554/cam/realmonitor?channel=1&subtype=0",
                # List form with ffmpeg: prefix and fragment
                "Garage_sub": [
                    "exec:/usr/bin/false",
                    "ffmpeg:rtsp://*:*@192.168.11.200:554/cam/realmonitor?channel=1&subtype=1#video=copy",
                ],
                # Chained local stream: FrontPorch_clean -> FrontPorch_raw -> physical IP
                "FrontPorch_clean": "ffmpeg:rtsp://127.0.0.1:8554/FrontPorch_raw#video=copy",
                "FrontPorch_raw": "rtsp://*:*@192.168.11.205:554/stream1",
                # Fallback by camera name when ffmpeg.inputs is empty
                "FallbackCam_sub": "rtsp://*:*@192.168.11.210:554/sub",
                # Cyclic stream to verify cycle protection
                "CycleA": "rtsp://127.0.0.1:8554/CycleB",
                "CycleB": "rtsp://localhost:8554/CycleA",
            }
        },
        "cameras": {
            "DirectCam": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://*:*@192.168.11.196:554/stream1",
                            "roles": ["record", "detect"],
                        }
                    ]
                }
            },
            "Garage": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://127.0.0.1:8554/Garage",
                            "roles": ["record"],
                        },
                        {
                            "path": "ffmpeg:rtsp://127.0.0.1:8554/Garage_sub#video=copy",
                            "roles": ["detect"],
                        },
                    ]
                }
            },
            "FrontPorch": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://127.0.0.1:8554/FrontPorch_clean",
                            "roles": ["detect"],
                        }
                    ]
                }
            },
            "FallbackCam": {"ffmpeg": {"inputs": []}},
            "CyclicCam": {
                "ffmpeg": {
                    "inputs": [
                        {"path": "rtsp://127.0.0.1:8554/CycleA", "roles": ["detect"]}
                    ]
                }
            },
        },
    }

    resolved = resolve_camera_ips(config)
    assert resolved == {
        "DirectCam": "192.168.11.196",
        "Garage": "192.168.11.200",
        "FrontPorch": "192.168.11.205",
        "FallbackCam": "192.168.11.210",
    }

    # Filtered by camera subset
    subset = resolve_camera_ips(config, cameras=["Garage", "UnknownCam"])
    assert subset == {"Garage": "192.168.11.200"}


def test_resolve_camera_ips_prioritizes_detect_role():
    config = {
        "cameras": {
            "MultiInputCam": {
                "ffmpeg": {
                    "inputs": [
                        {
                            "path": "rtsp://*:*@192.168.11.101:554/record_stream",
                            "roles": ["record"],
                        },
                        {
                            "path": "rtsp://*:*@192.168.11.102:554/detect_stream",
                            "roles": ["detect"],
                        },
                    ]
                }
            }
        }
    }
    assert resolve_camera_ips(config) == {"MultiInputCam": "192.168.11.102"}


@pytest.mark.parametrize(
    "malformed_config",
    [
        {},
        {"go2rtc": None, "cameras": None},
        {"go2rtc": {"streams": None}, "cameras": {"cam1": None}},
        {
            "go2rtc": {"streams": "not-a-dict"},
            "cameras": {
                "cam1": {
                    "ffmpeg": {
                        "inputs": [
                            None,
                            {"path": None, "roles": None},
                            {"path": 123, "roles": "not-a-list"},
                        ]
                    }
                }
            },
        },
    ],
)
def test_resolve_camera_ips_handles_malformed_config_safely(malformed_config):
    assert resolve_camera_ips(malformed_config) == {}


# ─────────────── 3. Watchdog State Machine ───────────────


def _make_stats(cameras_fps: dict[str, float | None], uptime: float | None = 120) -> dict:
    service = {"uptime": uptime} if uptime is not None else {}
    return {
        "service": service,
        "cameras": {
            cam: {"camera_fps": fps, "detection_fps": 0.0}
            for cam, fps in cameras_fps.items()
        },
    }


def test_watchdog_startup_grace_skips_and_resets_first_zero_ts():
    wd = CameraWatchdog(
        monitored_cameras=["Garage"],
        zero_fps_threshold=300,
        cooldown_seconds=900,
    )

    # Start a 0-FPS timer at t=1000 while Frigate is up (uptime=120)
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}, uptime=120), now=1000.0) == []
    # Also simulate an earlier reboot timestamp at t=500
    wd._states["Garage"].last_reboot_ts = 500.0
    assert wd._states["Garage"].first_zero_ts == 1000.0

    # Frigate restarts at t=1100 (uptime=20 < 60s): evaluation skipped AND first_zero_ts reset,
    # while last_reboot_ts is preserved.
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}, uptime=20), now=1100.0) == []
    assert wd._states["Garage"].first_zero_ts is None
    assert wd._states["Garage"].last_reboot_ts == 500.0

    # Also test missing / non-numeric / bool uptime
    wd._states["Garage"].first_zero_ts = 1150.0
    assert wd.evaluate_stats({"service": {}, "cameras": {"Garage": {"camera_fps": 0.0}}}, now=1200.0) == []
    assert wd._states["Garage"].first_zero_ts is None

    wd._states["Garage"].first_zero_ts = 1200.0
    assert wd.evaluate_stats({"service": {"uptime": True}, "cameras": {"Garage": {"camera_fps": 0.0}}}, now=1250.0) == []
    assert wd._states["Garage"].first_zero_ts is None

    # When Frigate uptime reaches >= 60s at t=1300, a fresh 300s window starts at 1300 (not 1000)
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}, uptime=65), now=1300.0) == []
    assert wd._states["Garage"].first_zero_ts == 1300.0
    # At t=1500 (200s since 1300, even though 500s since 1000): still < 300s, no reboot!
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}, uptime=265), now=1500.0) == []
    # At t=1600 (300s since 1300): triggers reboot!
    actions = wd.evaluate_stats(_make_stats({"Garage": 0.0}, uptime=365), now=1600.0)
    assert actions == [RebootAction(camera="Garage", zero_duration_seconds=300.0)]


def test_watchdog_zero_fps_threshold_and_recovery_reset():
    wd = CameraWatchdog(
        monitored_cameras=["Garage"],
        zero_fps_threshold=300,
        cooldown_seconds=900,
    )

    # Healthy (fps >= 0.1)
    assert wd.evaluate_stats(_make_stats({"Garage": 5.0}), now=1000.0) == []
    assert wd._states["Garage"].first_zero_ts is None

    # Drops to 0.0 at t=1000 -> starts timer
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1000.0) == []
    assert wd._states["Garage"].first_zero_ts == 1000.0

    # At t=1299 (299s < 300s) -> no action yet
    assert wd.evaluate_stats(_make_stats({"Garage": 0.05}), now=1299.0) == []

    # Brief recovery at t=1299.5 (fps = 0.1 >= 0.1) -> resets first_zero_ts
    assert wd.evaluate_stats(_make_stats({"Garage": 0.1}), now=1299.5) == []
    assert wd._states["Garage"].first_zero_ts is None

    # Drops to 0 again at t=2000 -> starts fresh 300s window
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=2000.0) == []
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=2299.0) == []
    actions = wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=2300.0)
    assert actions == [RebootAction(camera="Garage", zero_duration_seconds=300.0)]


def test_watchdog_cooldown_enforcement_and_mark_rebooted():
    wd = CameraWatchdog(
        monitored_cameras=["Garage"],
        zero_fps_threshold=300,
        cooldown_seconds=900,
    )

    # 0 FPS from t=1000 to t=1300 -> action triggered
    wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1000.0)
    actions = wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1300.0)
    assert len(actions) == 1

    # Mark rebooted at t=1300
    wd.mark_rebooted("Garage", now=1300.0)
    assert wd._states["Garage"].last_reboot_ts == 1300.0
    assert wd._states["Garage"].first_zero_ts == 1300.0

    # Camera stays at 0 FPS after reboot:
    # At t=1600 (300s of 0 FPS, but only 300s < 900s cooldown elapsed): blocked by cooldown
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1600.0) == []
    # At t=2199 (899s < 900s cooldown): still blocked by cooldown
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=2199.0) == []
    # At t=2200 (900s >= 900s cooldown and 900s >= 300s threshold): second reboot allowed
    actions2 = wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=2200.0)
    assert actions2 == [RebootAction(camera="Garage", zero_duration_seconds=900.0)]


def test_watchdog_cooldown_preserved_across_brief_recovery():
    wd = CameraWatchdog(
        monitored_cameras=["Garage"],
        zero_fps_threshold=300,
        cooldown_seconds=900,
    )
    wd.mark_rebooted("Garage", now=1000.0)

    # Camera recovers at t=1100
    assert wd.evaluate_stats(_make_stats({"Garage": 5.0}), now=1100.0) == []
    assert wd._states["Garage"].first_zero_ts is None
    assert wd._states["Garage"].last_reboot_ts == 1000.0

    # Camera drops back to 0 FPS at t=1200
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1200.0) == []
    # At t=1500 (300s at 0 FPS, but only 500s since reboot at t=1000 < 900s cooldown): blocked
    assert wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1500.0) == []
    # At t=1900 (900s since reboot at t=1000, 700s at 0 FPS): triggers
    actions = wd.evaluate_stats(_make_stats({"Garage": 0.0}), now=1900.0)
    assert actions == [RebootAction(camera="Garage", zero_duration_seconds=700.0)]


def test_mark_rebooted_defaults_now_to_current_time():
    wd = CameraWatchdog(monitored_cameras=["Garage"])
    wd.mark_rebooted("Garage")
    state = wd._states["Garage"]
    assert isinstance(state.last_reboot_ts, float)
    assert isinstance(state.first_zero_ts, float)


def test_watchdog_filters_monitored_cameras_and_handles_null_stats():
    wd = CameraWatchdog(monitored_cameras=["Garage"], zero_fps_threshold=300)
    stats = _make_stats({"Garage": 5.0, "UnmonitoredCam": 0.0})
    assert wd.evaluate_stats(stats, now=1000.0) == []
    assert wd.evaluate_stats(stats, now=1400.0) == []
    assert "UnmonitoredCam" not in wd._states

    # When monitored_cameras=None, monitors all cameras in stats
    wd_all = CameraWatchdog(monitored_cameras=None, zero_fps_threshold=300)
    wd_all.evaluate_stats(_make_stats({"CamA": 0.0}), now=1000.0)
    assert len(wd_all.evaluate_stats(_make_stats({"CamA": 0.0}), now=1300.0)) == 1

    # Null/malformed stats payloads do not raise
    assert wd.evaluate_stats(None, now=1000.0) == []  # type: ignore[arg-type]
    assert wd.evaluate_stats({"service": {"uptime": 120}, "cameras": None}, now=1000.0) == []
    assert (
        wd.evaluate_stats(
            {"service": {"uptime": 120}, "cameras": {"Garage": None}},
            now=1000.0,
        )
        == []
    )
    assert (
        wd.evaluate_stats(
            {"service": {"uptime": 120}, "cameras": {"Garage": {"camera_fps": None}}},
            now=1300.0,
        )
        != []
    )


# ─────────────── 4. Async HTTP Operations ───────────────


@pytest.mark.asyncio
async def test_reboot_camera_http_digest_handshake_success():
    requests_seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        assert request.url.host == "192.168.11.200"
        assert request.url.path == "/cgi-bin/magicBox.cgi"
        assert request.url.params.get("action") == "reboot"

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Digest "):
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Digest realm="Login to AMC0001", qop="auth", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"'
                },
            )
        assert 'username="admin"' in auth_header
        assert 'uri="/cgi-bin/magicBox.cgi?action=reboot"' in auth_header
        return httpx.Response(200, text="OK\r\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ok = await reboot_camera_http(client, "192.168.11.200", "admin", "secretpass")
        assert ok is True
        assert len(requests_seen) == 2


@pytest.mark.asyncio
async def test_reboot_camera_http_connection_drop_on_authenticated_request_is_success():
    """Amcrest/Dahua cameras often drop the TCP connection right after accepting
    the Digest-authenticated reboot request. That must be treated as True, whereas
    a connection drop on the initial unauthenticated challenge must return False."""
    call_count = 0

    async def drop_after_auth(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Digest realm="Amcrest", qop="auth", nonce="12345", opaque="abc"'
                },
            )
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(drop_after_auth)) as client:
        assert await reboot_camera_http(client, "192.168.11.200", "admin", "secretpass") is True
        assert call_count == 2


@pytest.mark.asyncio
async def test_reboot_camera_http_failures_and_no_credential_leak(caplog):
    # 1. ReadError on first (unauthenticated) request -> False
    async def drop_on_first(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("Connection reset by peer", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(drop_on_first)) as client:
        assert await reboot_camera_http(client, "192.168.11.200", "admin", "SuperSecretRtspPass123") is False

    # 2. ConnectError -> False
    async def connect_err(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("No route to host", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_err)) as client:
        assert await reboot_camera_http(client, "192.168.11.200", "admin", "SuperSecretRtspPass123") is False

    # 3. Non-200 response (e.g. 401 wrong credentials or 500) -> False
    async def always_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    with caplog.at_level("DEBUG"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(always_403)) as client:
            assert await reboot_camera_http(client, "192.168.11.200", "admin", "SuperSecretRtspPass123") is False

    assert "SuperSecretRtspPass123" not in caplog.text


@pytest.mark.asyncio
async def test_send_telegram_alert_and_no_token_leak(caplog):
    sent_payloads: list[dict] = []

    async def tg_ok(request: httpx.Request) -> httpx.Response:
        import json

        assert request.method == "POST"
        assert request.url.path == "/bot12345:SecretBotTokenXYZ/sendMessage"
        sent_payloads.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(tg_ok)) as client:
        # Missing token or chat_id -> returns False without calling API
        assert await send_telegram_alert(client, "", "999", "hello") is False
        assert await send_telegram_alert(client, "12345:SecretBotTokenXYZ", "", "hello") is False
        assert len(sent_payloads) == 0

        msg = format_reboot_message("Garage<Cam>", "192.168.11.200", 305, 900)
        assert "Garage&lt;Cam&gt;" in msg
        assert "192.168.11.200" in msg
        assert "5m 5s" in msg
        assert "15m" in msg

        ok = await send_telegram_alert(client, "12345:SecretBotTokenXYZ", "-100999", msg)
        assert ok is True
        assert len(sent_payloads) == 1
        assert sent_payloads[0]["chat_id"] == "-100999"
        assert sent_payloads[0]["parse_mode"] == "HTML"

    # Error handling and token non-leakage in logs
    async def tg_fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failure for api.telegram.org/bot12345:SecretBotTokenXYZ", request=request)

    caplog.clear()
    with caplog.at_level("DEBUG"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(tg_fail)) as client:
            ok = await send_telegram_alert(client, "12345:SecretBotTokenXYZ", "-100999", "hi")
            assert ok is False

    assert "SecretBotTokenXYZ" not in caplog.text


@pytest.mark.asyncio
async def test_run_watchdog_tick_end_to_end_and_partial_failure():
    """End-to-end test of run_watchdog_tick across multiple cameras:
    - CamFailIP: has no resolvable IP -> skipped, not marked rebooted
    - CamFailHTTP: reboot HTTP returns 500 -> not marked rebooted (retried next tick)
    - Garage: resolved via go2rtc -> Digest reboot succeeds -> marked rebooted + Telegram alert sent
    Also verifies run_watchdog_tick works when now=None."""
    rebooted_ips: list[str] = []
    telegram_messages: list[str] = []

    stats_payload = {
        "service": {"uptime": 600},
        "cameras": {
            "CamFailIP": {"camera_fps": 0.0},
            "CamFailHTTP": {"camera_fps": 0.0},
            "Garage": {"camera_fps": 0.0},
        },
    }
    config_payload = {
        "go2rtc": {
            "streams": {
                "Garage_sub": "rtsp://{FRIGATE_RTSP_USER}:{FRIGATE_RTSP_PASSWORD}@192.168.11.200:554/sub",
            }
        },
        "cameras": {
            "CamFailIP": {"ffmpeg": {"inputs": []}},
            "CamFailHTTP": {
                "ffmpeg": {
                    "inputs": [{"path": "rtsp://*:*@192.168.11.199:554/stream1", "roles": ["detect"]}]
                }
            },
            "Garage": {
                "ffmpeg": {
                    "inputs": [{"path": "rtsp://127.0.0.1:8554/Garage_sub", "roles": ["detect"]}]
                }
            },
        },
    }

    async def router(request: httpx.Request) -> httpx.Response:
        if request.url.host == "frigate" and request.url.path == "/api/stats":
            return httpx.Response(200, json=stats_payload)
        if request.url.host == "frigate" and request.url.path == "/api/config":
            return httpx.Response(200, json=config_payload)
        if request.url.host == "192.168.11.199" and request.url.path == "/cgi-bin/magicBox.cgi":
            return httpx.Response(500, text="Internal Error")
        if request.url.host == "192.168.11.200" and request.url.path == "/cgi-bin/magicBox.cgi":
            if "authorization" not in request.headers:
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": 'Digest realm="Amcrest", qop="auth", nonce="abc", opaque="xyz"'
                    },
                )
            rebooted_ips.append(request.url.host)
            return httpx.Response(200, text="OK\r\n")
        if request.url.host == "api.telegram.org":
            import json

            telegram_messages.append(json.loads(request.content.decode())["text"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    wd = CameraWatchdog(
        monitored_cameras=["CamFailIP", "CamFailHTTP", "Garage"],
        zero_fps_threshold=300,
        cooldown_seconds=900,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        # First tick at t=1000: starts 0-FPS timers, no reboots yet
        res1 = await run_watchdog_tick(
            client,
            wd,
            frigate_url="http://frigate:5000",
            rtsp_user="admin",
            rtsp_password="pw",
            bot_token="123:tok",
            chat_id="456",
            now=1000.0,
        )
        assert res1 == []
        assert rebooted_ips == []

        # Backdate first_zero_ts by 305s from real time so we can call run_watchdog_tick with now=None
        import time

        real_now = time.time()
        for cam in ("CamFailIP", "CamFailHTTP", "Garage"):
            wd._states[cam].first_zero_ts = real_now - 305.0

        res2 = await run_watchdog_tick(
            client,
            wd,
            frigate_url="http://frigate:5000",
            rtsp_user="admin",
            rtsp_password="pw",
            bot_token="123:tok",
            chat_id="456",
            now=None,
        )
        assert res2 == ["Garage"]
        assert rebooted_ips == ["192.168.11.200"]
        assert len(telegram_messages) == 1
        assert "Garage" in telegram_messages[0]
        assert "192.168.11.200" in telegram_messages[0]

        # Garage is now marked rebooted (in cooldown); CamFailIP and CamFailHTTP were NOT marked rebooted
        assert wd._states["Garage"].last_reboot_ts is not None
        assert wd._states["CamFailIP"].last_reboot_ts is None
        assert wd._states["CamFailHTTP"].last_reboot_ts is None
