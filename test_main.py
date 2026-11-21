import sys
from unittest.mock import MagicMock

# Mock dependencies that might be missing for standard unit test run
try:
    import httpx
except ImportError:
    sys.modules["httpx"] = MagicMock()

for mod in ["telegram", "telegram.constants", "telegram.ext", "dotenv"]:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            sys.modules[mod] = MagicMock()

import unittest
import os
from contextlib import contextmanager
from datetime import datetime, time as dt_time
from unittest.mock import AsyncMock, patch

# Set environment variables for main.py import
os.environ["FRIGATE_URL"] = "http://localhost:5000"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
os.environ["TELEGRAM_CHAT_ID"] = "fake"
os.environ["STATE_FILE"] = "state.json"

import main
import grouping


def _epoch_at(hour, minute):
    """Build a tz-aware epoch (per main._CACHED_TZ, i.e. TIMEZONE) for a
    given local hour/minute — never a bare naive datetime.timestamp(),
    which would silently use the test-runner machine's local timezone."""
    return datetime(2024, 1, 1, hour, minute, tzinfo=main._CACHED_TZ).timestamp()


def _make_stream_cm(chunks=(b"x" * 150,), headers=None, raise_exc=None, aiter_spy=None, stream_exc=None):
    """Build a mock async context manager matching httpx.AsyncClient.stream()."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = headers or {}
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status = MagicMock()

    async def _aiter_bytes(_chunk_size=65536):
        for chunk in chunks:
            if aiter_spy is not None:
                aiter_spy.append(len(chunk))
            yield chunk
        if stream_exc is not None:
            raise stream_exc

    resp.aiter_bytes = _aiter_bytes
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@contextmanager
def _night_alert_config(cameras, start=None, end=None):
    """Temporarily override NIGHT_ALERT_CAMERAS/START/END for a test, restoring
    the previous values on exit. start/end default to leaving the current
    value untouched (only cameras is required)."""
    orig = (main.NIGHT_ALERT_CAMERAS, main.NIGHT_ALERT_START, main.NIGHT_ALERT_END)
    main.NIGHT_ALERT_CAMERAS = cameras
    if start is not None:
        main.NIGHT_ALERT_START = start
    if end is not None:
        main.NIGHT_ALERT_END = end
    try:
        yield
    finally:
        main.NIGHT_ALERT_CAMERAS, main.NIGHT_ALERT_START, main.NIGHT_ALERT_END = orig


class TestMainLogic(unittest.TestCase):
    def test_format_caption_escaping(self):
        event = {
            "id": "123.456-abc\"",
            "camera": "Front <Door>",
            "label": "person & dog",
            "zones": ["zone1", "zone2 & 3"],
            "top_score": 0.88,
            "sub_label": "John <Doe>",
            "start_time": 1672531200,
        }
        main.EXTERNAL_URL = "https://example.com"
        caption = main.format_caption(event)

        self.assertIn("Front &lt;Door&gt;", caption)
        self.assertIn("person &amp; dog", caption)
        self.assertIn("zone1, zone2 &amp; 3", caption)
        self.assertIn("John &lt;Doe&gt;", caption)
        self.assertIn("https://example.com/events/123.456-abc&quot;", caption)

    def test_get_int_setting(self):
        os.environ["TEST_INT"] = "100"
        self.assertEqual(main.get_int_setting("TEST_INT", 50), 100)
        
        os.environ["TEST_INT"] = "not_an_int"
        self.assertEqual(main.get_int_setting("TEST_INT", 50), 50)
        
        if "TEST_INT" in os.environ:
            del os.environ["TEST_INT"]
        self.assertEqual(main.get_int_setting("TEST_INT", 50), 50)

    def test_get_bool_setting(self):
        tests = [
            ("true", True), ("1", True), ("yes", True), ("on", True),
            ("false", False), ("0", False), ("no", False), ("off", False),
            ("random", False)
        ]
        for val, expected in tests:
            os.environ["TEST_BOOL"] = val
            self.assertEqual(main.get_bool_setting("TEST_BOOL", not expected), expected)
            
        if "TEST_BOOL" in os.environ:
            del os.environ["TEST_BOOL"]
        self.assertEqual(main.get_bool_setting("TEST_BOOL", True), True)

    def test_parse_monitor_config(self):
        cases = [
            ("cam1:z1,z2;cam2:all", {"cam1": {"z1", "z2"}, "cam2": {"all"}}),
            ("cam1", {"cam1": {"all"}}),
            ("", {}),
            ("  ", {}),
            ("cam1: ", {"cam1": {"all"}}),
        ]
        for raw, expected in cases:
            self.assertEqual(main.parse_monitor_config(raw), expected)

    def test_parse_hhmm(self):
        cases = [
            ("22:00", dt_time(22, 0)),
            ("06:00", dt_time(6, 0)),
            ("00:00", dt_time(0, 0)),
            ("23:59", dt_time(23, 59)),
        ]
        for raw, expected in cases:
            self.assertEqual(main.parse_hhmm(raw, "22:00"), expected)

    def test_parse_hhmm_falls_back_to_default_on_invalid_input(self):
        with self.assertLogs(main.logger.name, level="WARNING"):
            result = main.parse_hhmm("not-a-time", "22:00")
        self.assertEqual(result, dt_time(22, 0))

    def test_in_night_window_non_wrapping(self):
        start, end = dt_time(9, 0), dt_time(17, 0)
        cases = [
            (dt_time(12, 0), True),
            (dt_time(8, 0), False),
            (dt_time(18, 0), False),
            (dt_time(9, 0), True),   # start boundary, inclusive
            (dt_time(17, 0), False),  # end boundary, exclusive
        ]
        for now_local, expected in cases:
            self.assertEqual(main.in_night_window(now_local, start, end), expected)

    def test_in_night_window_midnight_crossing(self):
        start, end = dt_time(22, 0), dt_time(6, 0)
        cases = [
            (dt_time(23, 0), True),
            (dt_time(2, 0), True),
            (dt_time(12, 0), False),
            (dt_time(22, 0), True),  # start boundary
            (dt_time(6, 0), False),  # end boundary, exclusive
        ]
        for now_local, expected in cases:
            self.assertEqual(main.in_night_window(now_local, start, end), expected)

    def test_format_caption_sub_label_dict(self):
        event = {
            "id": "123",
            "camera": "cam",
            "label": "person",
            "sub_label": {"label": "John", "score": 0.95},
            "top_score": 0.9,
            "start_time": 1672531200,
        }
        caption = main.format_caption(event)
        self.assertIn("John", caption)
        self.assertIn("95%", caption)

    def test_format_grouped_caption_unions_labels_and_zones(self):
        main.EXTERNAL_URL = ""
        data = {
            "camera": "Garage",
            "labels": ["person", "car"],
            "zones": ["driveway", "porch"],
            "sub_labels": [],
            "top_score": 0.91,
            "start_time": 1672531200,
            "end_time": 1672531260,
            "primary_event_id": "evt1",
        }
        caption = main.format_grouped_caption(data)

        self.assertIn("car, person", caption)
        self.assertIn("driveway, porch", caption)
        self.assertIn("91%", caption)

    def test_format_grouped_caption_multiple_recognized_names(self):
        main.EXTERNAL_URL = ""
        data = {
            "camera": "Garage",
            "labels": ["person"],
            "zones": [],
            "sub_labels": [("John", 0.9), ("Jane", None)],
            "top_score": 0.9,
            "start_time": 1672531200,
            "end_time": 1672531260,
            "primary_event_id": "evt1",
        }
        caption = main.format_grouped_caption(data)

        self.assertIn("John", caption)
        self.assertIn("90%", caption)
        self.assertIn("Jane", caption)
        self.assertIn("N/A", caption)  # zones is empty -> N/A

    def test_format_grouped_caption_escaping_and_link(self):
        main.EXTERNAL_URL = "https://example.com"
        data = {
            "camera": "Front <Door>",
            "labels": ["person & dog"],
            "zones": ["zone1 & 2"],
            "sub_labels": [("John <Doe>", 0.5)],
            "top_score": 0.5,
            "start_time": 1672531200,
            "end_time": None,
            "primary_event_id": "evt\"1",
        }
        caption = main.format_grouped_caption(data)

        self.assertIn("Front &lt;Door&gt;", caption)
        self.assertIn("person &amp; dog", caption)
        self.assertIn("zone1 &amp; 2", caption)
        self.assertIn("John &lt;Doe&gt;", caption)
        self.assertNotIn("🕑", caption)  # no end_time -> no End line
        self.assertIn("https://example.com/events/evt&quot;1", caption)

    def test_format_caption_sub_label_in_data(self):
        event = {
            "id": "123",
            "camera": "cam",
            "label": "person",
            "data": {"sub_label": "Jane"},
            "top_score": 0.9,
            "start_time": 1672531200,
        }
        caption = main.format_caption(event)
        self.assertIn("Jane", caption)

class TestAsyncLogic(unittest.IsolatedAsyncioTestCase):
    @patch("main.fetch_event_media", return_value=None)  # no event snapshot/clip/thumbnail available
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_fetches_clip_per_event_not_union_window(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """Regression: a group spanning 2 constituent events must fetch ONE
        clip per event over that event's own start/end window, not a single
        clip over the merged union window — Frigate's recording retention
        doesn't reliably back a window spanning the gap between events."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage",
            labels={"person"},
            review_ids=["rev1"],
            event_ids={"e1", "e2"},
            first_start=100,
            last_activity_end=200,
            last_seen_at=200,
        )

        async def details_side_effect(client, event_id):
            return {
                "e1": {"id": "e1", "label": "person", "zones": ["driveway"], "start_time": 100, "end_time": 130, "top_score": 0.8},
                "e2": {"id": "e2", "label": "person", "zones": ["porch"], "start_time": 150, "end_time": 200, "top_score": 0.9, "sub_label": "Found"},
            }[event_id]

        mock_details.side_effect = details_side_effect

        async def clip_side_effect(_client, camera, start, end, **_kwargs):
            return {
                (100 - main.CLIP_PADDING_SECONDS, 130 + main.CLIP_PADDING_SECONDS): b"e1_clip_bytes",
                (150 - main.CLIP_PADDING_SECONDS, 200 + main.CLIP_PADDING_SECONDS): b"e2_clip_bytes",
            }[(start, end)]

        mock_clip.side_effect = clip_side_effect
        mock_snap.return_value = b"snap_bytes"

        await main.send_grouped_notification(bot, group, http_client)

        # Two separate per-event fetches, each on that event's own tight
        # window — never one call spanning 100 -> 200 (the union).
        self.assertEqual(mock_clip.call_count, 2)
        mock_clip.assert_any_call(http_client, "Garage", 100 - main.CLIP_PADDING_SECONDS, 130 + main.CLIP_PADDING_SECONDS)
        mock_clip.assert_any_call(http_client, "Garage", 150 - main.CLIP_PADDING_SECONDS, 200 + main.CLIP_PADDING_SECONDS)

        # Two videos sent, chronologically ordered and tagged, aggregate
        # caption (union labels/zones/names) attached to both.
        self.assertEqual(bot.send_video.call_count, 2)
        call1, call2 = bot.send_video.call_args_list
        self.assertEqual(call1.kwargs["video"], b"e1_clip_bytes")
        self.assertIn("(Event 1/2)", call1.kwargs["caption"])
        self.assertEqual(call2.kwargs["video"], b"e2_clip_bytes")
        self.assertIn("(Event 2/2)", call2.kwargs["caption"])
        for call_kwargs in (call1.kwargs, call2.kwargs):
            self.assertIn("Found", call_kwargs["caption"])
            self.assertIn("driveway, porch", call_kwargs["caption"])

    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_sends_only_the_events_whose_clip_succeeded(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """One event's clip fails entirely (recording endpoint AND its own
        pre-generated clip both empty) while the other's succeeds — only
        the successful one is sent, untagged (a single successful clip
        doesn't get an "(Event N/M)" tag even though the group had 2)."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1", "e2"},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )

        async def details_side_effect(client, event_id):
            return {
                "e1": {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 130},
                "e2": {"id": "e2", "label": "person", "zones": [], "start_time": 150, "end_time": 200},
            }[event_id]

        mock_details.side_effect = details_side_effect
        mock_clip.return_value = None  # recording endpoint has nothing for either event

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            if media_type == "clip" and event_id == "e1":
                return b"e1_event_clip_bytes"
            return None  # e2's own pre-generated clip also fails

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        bot.send_video.assert_called_once()
        call_kwargs = bot.send_video.call_args.kwargs
        self.assertEqual(call_kwargs["video"], b"e1_event_clip_bytes")
        self.assertNotIn("(Event", call_kwargs["caption"])

    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_middle_event_failure_renumbers_around_gap(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """3-event group, the MIDDLE event's clip fails entirely (both
        recording endpoint and its own pre-generated clip) while the first
        and last succeed — they're still sent chronologically, tagged
        "(Event 1/2)"/"(Event 2/2)" by count of what's actually delivered,
        skipping the failed middle one rather than leaving a numbering gap."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1", "e2", "e3"},
            first_start=100, last_activity_end=300, last_seen_at=300,
        )

        async def details_side_effect(client, event_id):
            return {
                "e1": {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 130},
                "e2": {"id": "e2", "label": "person", "zones": [], "start_time": 150, "end_time": 180},
                "e3": {"id": "e3", "label": "person", "zones": [], "start_time": 250, "end_time": 300},
            }[event_id]

        mock_details.side_effect = details_side_effect

        async def clip_side_effect(_client, camera, start, end, **_kwargs):
            if (start, end) == (150 - main.CLIP_PADDING_SECONDS, 180 + main.CLIP_PADDING_SECONDS):
                return None  # e2's recording window fails
            return {
                (100 - main.CLIP_PADDING_SECONDS, 130 + main.CLIP_PADDING_SECONDS): b"e1_clip_bytes",
                (250 - main.CLIP_PADDING_SECONDS, 300 + main.CLIP_PADDING_SECONDS): b"e3_clip_bytes",
            }[(start, end)]

        mock_clip.side_effect = clip_side_effect
        mock_media.return_value = None  # e2's pre-generated clip fallback also fails
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        self.assertEqual(bot.send_video.call_count, 2)
        call1, call2 = bot.send_video.call_args_list
        self.assertEqual(call1.kwargs["video"], b"e1_clip_bytes")
        self.assertIn("(Event 1/2)", call1.kwargs["caption"])
        self.assertEqual(call2.kwargs["video"], b"e3_clip_bytes")
        self.assertIn("(Event 2/2)", call2.kwargs["caption"])

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_drops_oversized_unsplittable_event_keeps_other(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """One event's clip is oversized (fetched via the pre-generated-clip
        fallback) and its split-into-halves attempt also fails (recording
        endpoint has nothing for either half); another event's clip is
        normal size. Only the normal one is sent, and — since exactly one
        event actually contributes — it's untagged, not mislabeled
        "(Event 1/2)" for content that never arrives."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1", "e2"},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )

        async def details_side_effect(client, event_id):
            return {
                "e1": {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 130},
                "e2": {"id": "e2", "label": "person", "zones": [], "start_time": 150, "end_time": 200},
            }[event_id]

        mock_details.side_effect = details_side_effect
        mock_clip.return_value = None  # recording endpoint has nothing for either event

        oversized = b"x" * (main.MAX_TELEGRAM_FILE_SIZE + 1024)

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            if media_type != "clip":
                return None
            return oversized if event_id == "e1" else b"e2_event_clip_bytes"

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        bot.send_video.assert_called_once()
        call_kwargs = bot.send_video.call_args.kwargs
        self.assertEqual(call_kwargs["video"], b"e2_event_clip_bytes")
        self.assertNotIn("(Event", call_kwargs["caption"])

    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details", return_value=None)
    async def test_send_grouped_notification_oversized_clip_with_no_window_cannot_split(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """The event's own details fetch failed entirely (no start/end
        resolved at all), so its clip can only come from the pre-generated
        fallback with no window (`start=None` in `_fetch_event_clip`) — if
        that clip is oversized there's no window left to split it into
        halves, so it's dropped with a warning rather than crashing."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        # mock_details returns None -> details_list ends up empty -> the
        # events_for_clips fallback synthesizes (event_id, None, None).

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            if media_type == "clip":
                return b"x" * (main.MAX_TELEGRAM_FILE_SIZE + 1024)
            return None

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = b"snap_bytes"

        await main.send_grouped_notification(bot, group, http_client)

        mock_clip.assert_not_called()  # no window -> never tries the recording endpoint
        bot.send_photo.assert_called_once()  # oversized+unsplittable -> falls through to photo
        self.assertEqual(bot.send_photo.call_args.kwargs["photo"], b"snap_bytes")

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_event_tag_and_part_tag_combine(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """2-event group: event A's clip is normal size (1 part), event B's
        clip is oversized but splits successfully into 2 parts. The
        "(Event N/M)" tag and the "(Part X/2)" suffix must combine on B's
        two videos while A's stays untagged-by-part (single part)."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"eA", "eB"},
            first_start=100, last_activity_end=305, last_seen_at=305,
        )

        async def details_side_effect(client, event_id):
            return {
                "eA": {"id": "eA", "label": "person", "zones": [], "start_time": 100, "end_time": 110},
                "eB": {"id": "eB", "label": "person", "zones": [], "start_time": 200, "end_time": 300},
            }[event_id]

        mock_details.side_effect = details_side_effect

        oversized = b"x" * (main.MAX_TELEGRAM_FILE_SIZE + 1024)
        pad = main.CLIP_PADDING_SECONDS
        mid = (200 - pad) + ((300 + pad) - (200 - pad)) // 2  # 250

        async def clip_side_effect(_client, camera, start, end, **_kwargs):
            return {
                (100 - pad, 110 + pad): b"eventA_bytes",
                (200 - pad, 300 + pad): oversized,
                (200 - pad, mid): b"eventB_part1",
                (mid, 300 + pad): b"eventB_part2",
            }[(start, end)]

        mock_clip.side_effect = clip_side_effect
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        self.assertEqual(bot.send_video.call_count, 3)
        call1, call2, call3 = bot.send_video.call_args_list

        self.assertEqual(call1.kwargs["video"], b"eventA_bytes")
        self.assertIn("(Event 1/2)", call1.kwargs["caption"])
        self.assertNotIn("(Part", call1.kwargs["caption"])

        self.assertEqual(call2.kwargs["video"], b"eventB_part1")
        self.assertIn("(Event 2/2)", call2.kwargs["caption"])
        self.assertIn("(Part 1/2)", call2.kwargs["caption"])

        self.assertEqual(call3.kwargs["video"], b"eventB_part2")
        self.assertIn("(Event 2/2)", call3.kwargs["caption"])
        self.assertIn("(Part 2/2)", call3.kwargs["caption"])

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_partial_send_failure_does_not_send_duplicate_fallback(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """2-event group: the first video sends successfully, the second
        raises (e.g. a flood-control blip mid-loop). One video already
        reached the user, so no GIF/photo/text fallback should follow —
        that would be a confusing duplicate, not a recovery."""
        bot = MagicMock()
        bot.send_video = AsyncMock(side_effect=[None, Exception("flood control")])
        bot.send_animation = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1", "e2"},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )

        async def details_side_effect(client, event_id):
            return {
                "e1": {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 130},
                "e2": {"id": "e2", "label": "person", "zones": [], "start_time": 150, "end_time": 200},
            }[event_id]

        mock_details.side_effect = details_side_effect
        mock_clip.return_value = b"clip_bytes"
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        self.assertEqual(bot.send_video.call_count, 2)  # attempted both; 2nd raised
        bot.send_animation.assert_not_called()
        bot.send_photo.assert_not_called()
        bot.send_message.assert_not_called()

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_clamps_padding_at_zero(self, mock_details, mock_clip, mock_snap, mock_media):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=2, last_activity_end=10, last_seen_at=10,
        )
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 2, "end_time": 10}
        mock_clip.return_value = b"clip_bytes"
        mock_snap.return_value = b"snap_bytes"

        await main.send_grouped_notification(bot, group, http_client)

        # start_time=2 minus padding would go negative; must clamp to 0.
        mock_clip.assert_called_once_with(http_client, "Garage", 0, 10 + main.CLIP_PADDING_SECONDS)

    @patch('main.fetch_event_media', return_value=None)
    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_splits_into_two_clips_when_over_limit(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera='Backyard', labels={'person'}, review_ids=['rev1'], event_ids={'e1'},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )
        mock_details.return_value = {'id': 'e1', 'label': 'person', 'zones': [], 'start_time': 100, 'end_time': 200}

        def clip_side_effect(_client, camera, start, end, **_kwargs):
            if start == 100 - main.CLIP_PADDING_SECONDS and end == 200 + main.CLIP_PADDING_SECONDS:
                return b'x' * (main.MAX_TELEGRAM_FILE_SIZE + 1024)
            elif end <= (100 - main.CLIP_PADDING_SECONDS + 200 + main.CLIP_PADDING_SECONDS) // 2:
                return b'part1_bytes'
            else:
                return b'part2_bytes'

        mock_clip.side_effect = clip_side_effect
        mock_snap.return_value = b'snap_bytes'

        await main.send_grouped_notification(bot, group, http_client)

        self.assertEqual(bot.send_video.call_count, 2)
        call1 = bot.send_video.call_args_list[0]
        call2 = bot.send_video.call_args_list[1]
        self.assertEqual(call1.kwargs['video'], b'part1_bytes')
        self.assertIn('1/2', call1.kwargs['caption'])
        self.assertEqual(call2.kwargs['video'], b'part2_bytes')
        self.assertIn('2/2', call2.kwargs['caption'])

    @patch('main.fetch_event_media', return_value=None)
    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_handles_partial_split_success(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera='Backyard', labels={'person'}, review_ids=['rev1'], event_ids={'e1'},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )
        mock_details.return_value = {'id': 'e1', 'label': 'person', 'zones': [], 'start_time': 100, 'end_time': 200}

        def clip_side_effect(_client, camera, start, end, **_kwargs):
            if start == 100 - main.CLIP_PADDING_SECONDS and end == 200 + main.CLIP_PADDING_SECONDS:
                return b'x' * (main.MAX_TELEGRAM_FILE_SIZE + 1024)
            elif end <= (100 - main.CLIP_PADDING_SECONDS + 200 + main.CLIP_PADDING_SECONDS) // 2:
                return b'part1_bytes'
            else:
                return b'x' * (main.MAX_TELEGRAM_FILE_SIZE + 1024) # part2 too large

        mock_clip.side_effect = clip_side_effect
        mock_snap.return_value = b'snap_bytes'

        await main.send_grouped_notification(bot, group, http_client)

        self.assertEqual(bot.send_video.call_count, 1)
        self.assertEqual(bot.send_video.call_args.kwargs['video'], b'part1_bytes')

    @patch('main.fetch_event_media', return_value=None)
    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_falls_back_to_photo_if_split_parts_still_too_large(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera='Backyard', labels={'person'}, review_ids=['rev1'], event_ids={'e1'},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )
        mock_details.return_value = {'id': 'e1', 'label': 'person', 'zones': [], 'start_time': 100, 'end_time': 200}
        mock_clip.return_value = b'x' * (main.MAX_TELEGRAM_FILE_SIZE + 1024)
        mock_snap.return_value = b'snap_bytes'

        await main.send_grouped_notification(bot, group, http_client)

        bot.send_video.assert_not_called()
        bot.send_photo.assert_called_once()
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'], b'snap_bytes')

    # ── N-part clip split ──────────────────────────────────────────────
    # MAX_TELEGRAM_FILE_SIZE is patched to 1000 bytes so fixtures stay tiny.
    # Planned parts = max(2, ceil(size / (MAX * 0.8))) clamped to the window
    # length; at most main.MAX_CLIP_PARTS (10) are fetched.

    def test_max_clip_parts_is_ten(self):
        self.assertEqual(main.MAX_CLIP_PARTS, 10)

    def test_split_windows_two_parts_matches_legacy_midpoint(self):
        for start, end in [(95, 205), (95, 206), (0, 3)]:
            mid = start + (end - start) // 2
            self.assertEqual(main._split_windows(start, end, 2), [(start, mid), (mid, end)])

    def test_split_windows_five_parts_are_contiguous_and_exact(self):
        self.assertEqual(
            main._split_windows(95, 205, 5),
            [(95, 117), (117, 139), (139, 161), (161, 183), (183, 205)],
        )
        for start, end, n in [(0, 7, 5), (10, 22, 12), (95, 206, 5)]:
            windows = main._split_windows(start, end, n)
            self.assertEqual(len(windows), n)
            self.assertEqual(windows[0][0], start)
            self.assertEqual(windows[-1][1], end)
            for (_, a_end), (b_start, _) in zip(windows, windows[1:]):
                self.assertEqual(a_end, b_start)
            for w_start, w_end in windows:
                self.assertGreaterEqual(w_end - w_start, 1)

    def test_planned_part_count(self):
        # Real limit: the legacy "MAX + 1 KiB" fixture still plans 2 parts.
        self.assertEqual(main._planned_part_count(main.MAX_TELEGRAM_FILE_SIZE + 1024, 110), 2)
        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            self.assertEqual(main._planned_part_count(1001, 110), 2)
            self.assertEqual(main._planned_part_count(4200, 110), 6)  # ceil(4.2 / 0.8)
            self.assertEqual(main._planned_part_count(3800, 110), 5)  # ceil(4.75 / 0.8)
            self.assertEqual(main._planned_part_count(10500, 110), 14)  # ceil(10.5 / 0.8)
            self.assertEqual(main._planned_part_count(4200, 3), 3)  # clamped by duration

    def _long_single_event_group(self):
        return grouping.PendingGroup(
            camera="Backyard", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )

    def _padded_window(self):
        pad = main.CLIP_PADDING_SECONDS
        return 100 - pad, 200 + pad

    @staticmethod
    def _expected_windows(start, end, n):
        d = end - start
        return [(start + i * d // n, start + (i + 1) * d // n) for i in range(n)]

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_splits_into_n_parts(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 200}
        mock_snap.return_value = b"snap_bytes"

        ps, pe = self._padded_window()
        windows = self._expected_windows(ps, pe, 5)
        responses = {(ps, pe): b"x" * 3800}  # ceil(3.8 / 0.8) = 5 parts
        responses.update({w: f"part{i}".encode() for i, w in enumerate(windows, start=1)})

        async def clip_side_effect(_client, camera, start, end, **_kwargs):
            return responses.get((start, end))

        mock_clip.side_effect = clip_side_effect

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            await main.send_grouped_notification(bot, self._long_single_event_group(), http_client)

        fetched = [(c.args[2], c.args[3]) for c in mock_clip.call_args_list]
        self.assertEqual(fetched[0], (ps, pe))
        self.assertEqual(sorted(fetched[1:]), sorted(windows))
        # Parts come from an already-fetched recording: no retry loop per part.
        self.assertTrue(all(c.kwargs.get("max_retries") == 1 for c in mock_clip.call_args_list[1:]))

        self.assertEqual(bot.send_video.call_count, 5)
        for i, call in enumerate(bot.send_video.call_args_list, start=1):
            self.assertEqual(call.kwargs["video"], f"part{i}".encode())
            self.assertIn(f"(Part {i}/5)", call.kwargs["caption"])
            self.assertNotIn("truncated", call.kwargs["caption"])
        bot.send_photo.assert_not_called()

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_truncates_to_max_clip_parts(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 200}
        mock_snap.return_value = b"snap_bytes"

        ps, pe = self._padded_window()
        windows = self._expected_windows(ps, pe, 12)
        responses = {(ps, pe): b"x" * 9000}  # ceil(9.0 / 0.8) = 12 parts
        responses.update({w: f"part{i}".encode() for i, w in enumerate(windows, start=1)})

        async def clip_side_effect(_client, camera, start, end, **_kwargs):
            return responses.get((start, end))

        mock_clip.side_effect = clip_side_effect

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            await main.send_grouped_notification(bot, self._long_single_event_group(), http_client)

        # Full clip + exactly the first 10 planned parts; parts 11-12 never fetched.
        self.assertEqual(mock_clip.call_count, 11)
        fetched = [(c.args[2], c.args[3]) for c in mock_clip.call_args_list]
        self.assertEqual(sorted(fetched[1:]), sorted(windows[:10]))

        self.assertEqual(bot.send_video.call_count, 10)
        calls = bot.send_video.call_args_list
        for i, call in enumerate(calls, start=1):
            self.assertEqual(call.kwargs["video"], f"part{i}".encode())
            self.assertIn(f"(Part {i}/12)", call.kwargs["caption"])
        for call in calls[:-1]:
            self.assertNotIn("truncated", call.kwargs["caption"])
        last_caption = calls[-1].kwargs["caption"]
        self.assertIn("clip truncated", last_caption)
        self.assertIn("10 of 12", last_caption)

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_skips_bad_middle_parts_with_warning(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 200}
        mock_snap.return_value = b"snap_bytes"

        ps, pe = self._padded_window()
        windows = self._expected_windows(ps, pe, 5)
        responses = {(ps, pe): b"x" * 3800}  # ceil(3.8 / 0.8) = 5 parts
        responses.update({w: f"part{i}".encode() for i, w in enumerate(windows, start=1)})
        responses[windows[1]] = b"x" * 1001  # part 2 still oversized
        responses[windows[2]] = None  # part 3 missing

        async def clip_side_effect(_client, camera, start, end, **_kwargs):
            return responses.get((start, end))

        mock_clip.side_effect = clip_side_effect

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            with self.assertLogs("frigate-telegram", level="WARNING") as logs:
                await main.send_grouped_notification(bot, self._long_single_event_group(), http_client)

        self.assertEqual(bot.send_video.call_count, 3)
        calls = bot.send_video.call_args_list
        self.assertEqual([c.kwargs["video"] for c in calls], [b"part1", b"part4", b"part5"])
        self.assertIn("(Part 1/5)", calls[0].kwargs["caption"])
        self.assertIn("(Part 4/5)", calls[1].kwargs["caption"])
        self.assertIn("(Part 5/5)", calls[2].kwargs["caption"])
        # One warning per dropped part, naming the event.
        part_warnings = [m for m in logs.output if "WARNING" in m and "e1" in m]
        self.assertGreaterEqual(len(part_warnings), 2)

    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_does_not_split_oversized_event_clip_fallback(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """Oversized data from the event clip.mp4 fallback (recording endpoint
        returned nothing) must not be time-split via the recording endpoint —
        recordings are evidently unavailable for that window."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 200}
        mock_snap.return_value = None
        mock_clip.return_value = None  # recording endpoint: nothing

        async def media_side_effect(_client, event_id, kind):
            return {"clip": b"x" * 4200, "snapshot": b"snap_bytes"}.get(kind)

        mock_media.side_effect = media_side_effect

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            with self.assertLogs("frigate-telegram", level="WARNING"):
                await main.send_grouped_notification(bot, self._long_single_event_group(), http_client)

        ps, pe = self._padded_window()
        mock_clip.assert_called_once_with(http_client, "Backyard", ps, pe)
        bot.send_video.assert_not_called()
        bot.send_photo.assert_called_once()
        self.assertEqual(bot.send_photo.call_args.kwargs["photo"], b"snap_bytes")

    @patch('main.fetch_event_media', return_value=None)
    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_falls_back_to_photo_on_send_video_exception(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock(side_effect=Exception('Request Entity Too Large (413)'))
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera='Backyard', labels={'person'}, review_ids=['rev1'], event_ids={'e1'},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {'id': 'e1', 'label': 'person', 'zones': [], 'start_time': 100, 'end_time': 110}
        mock_clip.return_value = b'clip_bytes'
        mock_snap.return_value = b'snap_bytes'

        await main.send_grouped_notification(bot, group, http_client)

        bot.send_photo.assert_called_once()
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'], b'snap_bytes')

    @patch("main.fetch_event_media", return_value=None)  # no event clip/snapshot/thumbnail -> stays on photo fallback
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_photo_send_failure_does_not_double_send(
        self, mock_details, mock_clip, mock_snap, mock_media
    ):
        """Regression: when the clip is unavailable (photo fallback branch)
        and bot.send_photo itself raises, the failure must propagate rather
        than being caught by the outer send_video except-block and re-sent
        a second time with a misleading '(Video upload failed...)' caption
        — no video was ever attempted on this path."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=Exception("Telegram photo upload failed"))
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"car"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "car", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = None  # no clip -> photo fallback branch
        mock_snap.return_value = b"snap_bytes"

        with self.assertRaises(Exception):
            await main.send_grouped_notification(bot, group, http_client)

        self.assertEqual(bot.send_photo.call_count, 1)

    @patch("main.fetch_event_media", return_value=None)  # no event clip/snapshot/thumbnail -> live camera snapshot used
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_falls_back_to_photo(self, mock_details, mock_clip, mock_snap, mock_media):
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"car"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "car", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = None  # clip fetch failed
        mock_snap.return_value = b"snap_bytes"

        await main.send_grouped_notification(bot, group, http_client)

        bot.send_photo.assert_called_once()
        self.assertEqual(bot.send_photo.call_args.kwargs["photo"], b"snap_bytes")

    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_falls_back_to_text(
        self, mock_details, mock_clip, mock_snap, mock_thumb
    ):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"car"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "car", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = None
        mock_snap.return_value = None
        mock_thumb.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        bot.send_message.assert_called_once()

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_event_media")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_prefers_event_snapshot_over_live_camera_frame(
        self, mock_details, mock_clip, mock_media, mock_snap
    ):
        """Regression: the photo fallback must be event-anchored (Frigate's
        own snapshot.jpg for the event), not a live re-fetch of the camera's
        *current* frame — which by send time (well after the event ended)
        may no longer show the subject at all."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = b"clip_bytes"

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            return b"event_snapshot_bytes" if media_type == "snapshot" else None

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = b"live_frame_bytes"  # must NOT be used — event snapshot available

        await main.send_grouped_notification(bot, group, http_client)

        mock_media.assert_any_call(http_client, "e1", "snapshot")
        mock_snap.assert_not_called()
        self.assertEqual(bot.send_video.call_args.kwargs["thumbnail"], b"event_snapshot_bytes")

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_event_media")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_falls_back_to_thumbnail_then_live_frame(
        self, mock_details, mock_clip, mock_media, mock_snap
    ):
        """When the event has no snapshot.jpg, fall back to its thumbnail.jpg
        before ever touching the live camera frame."""
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"car"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "car", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = None

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            return b"thumbnail_bytes" if media_type == "thumbnail" else None

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = b"live_frame_bytes"  # must NOT be used — thumbnail available

        await main.send_grouped_notification(bot, group, http_client)

        mock_snap.assert_not_called()
        bot.send_photo.assert_called_once()
        self.assertEqual(bot.send_photo.call_args.kwargs["photo"], b"thumbnail_bytes")

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_event_media")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_clip_falls_back_to_event_clip_before_photo(
        self, mock_details, mock_clip, mock_media, mock_snap
    ):
        """Regression: if the recording endpoint has nothing yet (not-flushed
        segment / retention gap), try Frigate's pre-generated event clip
        before giving up and sending a photo instead of a video."""
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = None  # recording endpoint has nothing yet

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            return b"event_clip_bytes" if media_type == "clip" else None

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        mock_media.assert_any_call(http_client, "e1", "clip")
        bot.send_video.assert_called_once()
        self.assertEqual(bot.send_video.call_args.kwargs["video"], b"event_clip_bytes")

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_event_media")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_falls_back_to_gif_when_no_clip(
        self, mock_details, mock_clip, mock_media, mock_snap
    ):
        """GIF sits between video and photo: when no clip is available at
        all (recording endpoint and event clip both empty), the event's
        preview.gif is preferred over a still photo."""
        bot = MagicMock()
        bot.send_animation = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = None  # no clip anywhere

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            if media_type == "gif":
                return b"gif_bytes"
            if media_type == "snapshot":
                return b"event_snapshot_bytes"  # available too, but must lose to GIF
            return None

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        mock_media.assert_any_call(http_client, "e1", "gif")
        bot.send_animation.assert_called_once()
        self.assertEqual(bot.send_animation.call_args.kwargs["animation"], b"gif_bytes")
        bot.send_photo.assert_not_called()

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_event_media")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_falls_back_to_gif_on_send_video_exception(
        self, mock_details, mock_clip, mock_media, mock_snap
    ):
        """When a clip DID exist but bot.send_video raised, the fallback
        also tries GIF before photo — same tier order as the no-clip case."""
        bot = MagicMock()
        bot.send_video = AsyncMock(side_effect=Exception("Request Entity Too Large (413)"))
        bot.send_animation = AsyncMock()
        bot.send_photo = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera="Garage", labels={"person"}, review_ids=["rev1"], event_ids={"e1"},
            first_start=100, last_activity_end=110, last_seen_at=110,
        )
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 110}
        mock_clip.return_value = b"clip_bytes"  # clip existed; upload failed instead

        async def media_side_effect(_client, event_id, media_type, **_kwargs):
            return b"gif_bytes" if media_type == "gif" else None

        mock_media.side_effect = media_side_effect
        mock_snap.return_value = None

        await main.send_grouped_notification(bot, group, http_client)

        mock_media.assert_any_call(http_client, "e1", "gif")
        bot.send_animation.assert_called_once()
        self.assertEqual(bot.send_animation.call_args.kwargs["animation"], b"gif_bytes")
        bot.send_photo.assert_not_called()

    @patch("main._http_auth")
    async def test_fetch_recent_events(self, mock_auth):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        # Mock returns a list of events
        mock_resp.json.return_value = [{"id": "event_123", "camera": "cam1"}]
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        events = await main.fetch_recent_events(mock_client, "cam1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "event_123")

        # Verify params
        args, kwargs = mock_client.get.call_args
        self.assertEqual(kwargs["params"]["camera"], "cam1")
        self.assertEqual(kwargs["params"]["limit"], 5)
        self.assertEqual(kwargs["params"]["has_clip"], 1)

    async def test_fetch_recording_clip_url(self):
        mock_client = MagicMock()
        mock_client.stream.return_value = _make_stream_cm(chunks=(b"x" * 150,))

        data = await main.fetch_recording_clip(mock_client, "cam1", 1000, 1030)

        self.assertEqual(data, b"x" * 150)
        args, _kwargs = mock_client.stream.call_args
        self.assertEqual(args[0], "GET")
        url = args[1]
        # Should NOT have /recordings/
        self.assertNotIn("/recordings/", url)
        # main.FRIGATE_URL is http://localhost:5000 in test setup
        self.assertIn("/api/cam1/start/1000/end/1030/clip.mp4", url)

    async def test_fetch_media_with_retry_aborts_when_content_length_exceeds_limit(self):
        iterated = []
        mock_client = MagicMock()
        mock_client.stream.return_value = _make_stream_cm(
            headers={"content-length": "5000"},
            chunks=(b"x" * 5000,),
            aiter_spy=iterated,
        )

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            res = await main.fetch_media_with_retry(mock_client, "http://localhost/clip.mp4", "clip")

        self.assertTrue(res)
        self.assertNotIsInstance(res, bytes)
        self.assertEqual(len(res), 5000)
        self.assertEqual(iterated, [])

    async def test_fetch_media_with_retry_discards_buffer_when_stream_exceeds_limit(self):
        mock_client = MagicMock()
        mock_client.stream.return_value = _make_stream_cm(
            headers={},
            chunks=(b"a" * 600, b"b" * 600, b"c" * 300),
        )

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            res = await main.fetch_media_with_retry(mock_client, "http://localhost/clip.mp4", "clip")

        self.assertTrue(res)
        self.assertNotIsInstance(res, bytes)
        self.assertEqual(len(res), 1500)

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot", return_value=b"snap")
    @patch("main.fetch_recording_clip", return_value=b"clip_bytes")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_ongoing_event_uses_now_for_recording_window(
        self, mock_details, mock_clip, _mock_snap, _mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()
        mock_details.return_value = {
            "id": "e1",
            "label": "person",
            "zones": [],
            "start_time": 900,
            "end_time": None,
        }
        group = grouping.PendingGroup(
            camera="Backyard",
            labels={"person"},
            review_ids=["rev1"],
            event_ids={"e1"},
            first_start=900,
            last_activity_end=900,
            last_seen_at=900,
        )

        with patch("main.time.time", return_value=1000.0):
            await main.send_grouped_notification(bot, group, http_client)

        pad = main.CLIP_PADDING_SECONDS
        mock_clip.assert_called_once_with(http_client, "Backyard", 900 - pad, 1000 + pad)
        bot.send_video.assert_called_once()

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot", return_value=b"snap_bytes")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_streams_split_parts_without_buffering_all_in_ram(
        self, mock_details, mock_clip, _mock_snap, _mock_media
    ):
        order = []
        bot = MagicMock()

        async def send_side_effect(**kwargs):
            order.append(("send", kwargs["video"]))

        bot.send_video = AsyncMock(side_effect=send_side_effect)
        http_client = MagicMock()
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 200}

        ps, pe = self._padded_window()
        windows = self._expected_windows(ps, pe, 5)
        responses = {(ps, pe): b"x" * 3800}
        responses.update({w: f"part{i}".encode() for i, w in enumerate(windows, start=1)})

        async def clip_side_effect(_client, _camera, start, end, **_kwargs):
            order.append(("fetch", start, end))
            return responses.get((start, end))

        mock_clip.side_effect = clip_side_effect

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            await main.send_grouped_notification(bot, self._long_single_event_group(), http_client)

        send_part1_idx = order.index(("send", b"part1"))
        fetch_part3_idx = order.index(("fetch", windows[2][0], windows[2][1]))
        self.assertLess(send_part1_idx, fetch_part3_idx)

    @patch("asyncio.sleep")
    async def test_fetch_media_with_retry_retries_404_network_and_small_body(self, _mock_sleep):
        req = httpx.Request("GET", "http://localhost/clip.mp4")
        not_found_resp = httpx.Response(404, request=req)
        mock_client = MagicMock()
        mock_client.stream.side_effect = [
            _make_stream_cm(raise_exc=httpx.HTTPStatusError("404", request=req, response=not_found_resp)),
            _make_stream_cm(chunks=(b"a" * 50,), stream_exc=httpx.RequestError("drop", request=req)),
            _make_stream_cm(chunks=(b"tiny",)),
            _make_stream_cm(chunks=(b"x" * 150,)),
        ]

        res = await main.fetch_media_with_retry(
            mock_client, "http://localhost/clip.mp4", "clip", max_retries=4
        )
        self.assertEqual(res, b"x" * 150)
        self.assertEqual(mock_client.stream.call_count, 4)

    async def test_fetch_media_with_retry_ignores_non_ascii_or_invalid_content_length(self):
        for bad_cl in ("abc", "-5", "²"):
            mock_client = MagicMock()
            mock_client.stream.return_value = _make_stream_cm(
                headers={"content-length": bad_cl},
                chunks=(b"x" * 150,),
            )
            res = await main.fetch_media_with_retry(mock_client, "http://localhost/clip.mp4", "clip")
            self.assertEqual(res, b"x" * 150)

    async def test_fetch_camera_snapshot_returns_none_on_oversized_and_cmd_photo_handles(self):
        mock_client = MagicMock()
        mock_client.stream.return_value = _make_stream_cm(
            headers={"content-length": "5000"},
            chunks=(b"x" * 5000,),
        )
        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            snap = await main.fetch_camera_snapshot(mock_client, "Garage")
        self.assertIsNone(snap)

        update = AsyncMock()
        update.effective_chat.id = main.TELEGRAM_CHAT_ID
        context = MagicMock()
        context.args = ["Garage"]
        context.bot_data = {"http_client": mock_client}
        mock_client.stream.return_value = _make_stream_cm(
            headers={"content-length": "5000"},
            chunks=(b"x" * 5000,),
        )
        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            await main.cmd_photo(update, context)
        update.effective_chat.send_photo.assert_not_called()
        update.effective_chat.send_message.assert_called_once()

        update_all = AsyncMock()
        update_all.effective_chat.id = main.TELEGRAM_CHAT_ID
        context_all = MagicMock()
        context_all.bot_data = {"http_client": mock_client}
        mock_client.stream.return_value = _make_stream_cm(
            headers={"content-length": "5000"},
            chunks=(b"x" * 5000,),
        )
        with patch("main.fetch_camera_list", return_value=["Garage"]), patch.object(
            main, "MAX_TELEGRAM_FILE_SIZE", 1000
        ):
            await main.cmd_photo_all(update_all, context_all)
        update_all.effective_chat.send_photo.assert_not_called()
        update_all.effective_chat.send_message.assert_called_once()

    @patch("asyncio.sleep")
    @patch("main.fetch_recording_clip", return_value=None)
    @patch("main.fetch_event_details", return_value=None)
    @patch("main.fetch_event_media")
    async def test_fetch_video_data_robust_breaks_early_on_oversized_event_clip(
        self, mock_media, _mock_details, _mock_clip, mock_sleep
    ):
        mock_media.return_value = main._OversizedMedia(9999)
        res = await main.fetch_video_data_robust(MagicMock(), "Backyard", event_id="e1")
        self.assertIsNone(res)
        mock_media.assert_called_once()
        mock_sleep.assert_not_called()

    @patch("main.fetch_event_media", return_value=None)
    @patch("main.fetch_camera_snapshot", return_value=b"snap")
    @patch("main.fetch_recording_clip", return_value=b"clip_bytes")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_ongoing_event_clamps_inverted_clock(
        self, mock_details, mock_clip, _mock_snap, _mock_media
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()
        mock_details.return_value = {
            "id": "e1",
            "label": "person",
            "zones": [],
            "start_time": 1100,
            "end_time": None,
        }
        group = grouping.PendingGroup(
            camera="Backyard",
            labels={"person"},
            review_ids=["rev1"],
            event_ids={"e1"},
            first_start=1100,
            last_activity_end=1100,
            last_seen_at=1100,
        )

        with patch("main.time.time", return_value=1000.0):
            await main.send_grouped_notification(bot, group, http_client)

        pad = main.CLIP_PADDING_SECONDS
        mock_clip.assert_called_once_with(http_client, "Backyard", 1100 - pad, 1100 - pad)

    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot", return_value=b"snap_bytes")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_split_upload_failure_first_vs_second_part(
        self, mock_details, mock_clip, _mock_snap, mock_media
    ):
        http_client = MagicMock()
        mock_details.return_value = {"id": "e1", "label": "person", "zones": [], "start_time": 100, "end_time": 200}
        ps, pe = self._padded_window()
        windows = self._expected_windows(ps, pe, 5)
        responses = {(ps, pe): b"x" * 3800}
        responses.update({w: f"part{i}".encode() for i, w in enumerate(windows, start=1)})
        mock_clip.side_effect = lambda _c, _cam, s, e, **_kw: responses.get((s, e))

        # 1. Failure on Part 1 triggers GIF fallback without sending duplicate fallback photo
        bot1 = MagicMock()
        bot1.send_video = AsyncMock(side_effect=RuntimeError("upload failed"))
        bot1.send_animation = AsyncMock()
        bot1.send_photo = AsyncMock()
        mock_media.side_effect = lambda _c, _eid, kind: b"gif_bytes" if kind == "gif" else None

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            await main.send_grouped_notification(bot1, self._long_single_event_group(), http_client)
        bot1.send_animation.assert_called_once()
        bot1.send_photo.assert_not_called()

        # 2. Failure on Part 2 after Part 1 succeeded must NOT send fallback GIF or photo
        bot2 = MagicMock()
        bot2.send_video = AsyncMock(side_effect=[None, RuntimeError("part2 failed")])
        bot2.send_animation = AsyncMock()
        bot2.send_photo = AsyncMock()
        mock_media.reset_mock()
        mock_media.side_effect = None
        mock_media.return_value = None

        with patch.object(main, "MAX_TELEGRAM_FILE_SIZE", 1000):
            await main.send_grouped_notification(bot2, self._long_single_event_group(), http_client)
        self.assertEqual(bot2.send_video.call_count, 2)
        bot2.send_animation.assert_not_called()
        bot2.send_photo.assert_not_called()

    @patch("main.trigger_manual_event")
    @patch("main.fetch_event_media")
    @patch("asyncio.sleep")  # skip waiting
    async def test_cmd_video_manual_trigger(self, mock_sleep, mock_media, mock_trigger):
        # Setup context
        update = AsyncMock()
        update.effective_chat.id = "fake"
        # main.py now uses effective_message
        effective_message = AsyncMock()
        update.effective_message = effective_message
        
        context = MagicMock()
        context.args = ["garage"]
        context.bot_data = {"http_client": MagicMock()}

        # Mocks
        mock_trigger.return_value = "evt_123"
        # Simulate race condition: 2 failures then success
        mock_media.side_effect = [None, None, b"video_bytes"]
        
        # Bypass authorized_only
        with patch("main.TELEGRAM_CHAT_ID", "fake"): 
            update.effective_user.id = "fake"
            update.effective_chat.id = "fake"
            await main.cmd_video(update, context)

        # Verify
        mock_trigger.assert_called_once()
        # Should be called multiple times due to retry
        self.assertEqual(mock_media.call_count, 3) 
        mock_media.assert_called_with(context.bot_data["http_client"], "evt_123", "clip", max_retries=1)
        
        # Ensure we sent a video via effective_chat
        update.effective_chat.send_video.assert_called_once()

    @patch("main.fetch_event_media")
    @patch("main.fetch_event_details")
    @patch("main.fetch_recording_clip")
    @patch("asyncio.sleep", return_value=None)
    async def test_fetch_video_data_robust_max_retries_propagate(self, mock_sleep, mock_recording, mock_details, mock_media):
        """Test fetch_video_data_robust propagates max_retries=1 to fetch_event_media."""
        client = MagicMock()
        mock_media.return_value = b"event_clip"

        await main.fetch_video_data_robust(client, "cam1", "evt1")

        # Verify it was called with max_retries=1
        mock_media.assert_called_with(client, "evt1", "clip", max_retries=1)

    @patch("main.get_camera_selection_menu")
    async def test_cmd_video_menu(self, mock_get_menu):
        # Setup context
        update = MagicMock()
        # effective_chat used for reply
        update.effective_chat.send_message = AsyncMock()
        
        context = MagicMock()
        context.args = [] # No camera arg
        context.bot_data = {"http_client": MagicMock()}
        
        mock_menu = MagicMock()
        mock_get_menu.return_value = mock_menu

        with patch("main.TELEGRAM_CHAT_ID", 12345):
             update.effective_chat.id = 12345
             await main.cmd_video(update, context)

        # Verify
        update.effective_chat.send_message.assert_called_once()
        args, kwargs = update.effective_chat.send_message.call_args
        self.assertEqual(kwargs["reply_markup"], mock_menu)
        self.assertIn("Select a camera", args[0])

    @patch("main.cmd_photo_all")
    @patch("main.cmd_photo")
    @patch("main.get_main_menu")
    @patch("main.get_camera_selection_menu")
    async def test_button_handler_logic(self, mock_cam_menu, mock_main_menu, mock_cmd_photo, mock_cmd_photo_all):
        """Test the new button_handler navigation and command logic."""
        update = MagicMock()
        update.effective_chat.id = os.environ.get("TELEGRAM_CHAT_ID")
        update.callback_query = AsyncMock()
        update.effective_chat.id = "fake"
        context = MagicMock()
        context.bot_data = {"http_client": MagicMock()}

        # 1. Test Navigation to Snapshot Menu
        update.callback_query.data = "nav:snapshot"
        mock_cam_menu.return_value = MagicMock()
        await main.button_handler(update, context)
        update.callback_query.edit_message_text.assert_called()
        self.assertIn("Snapshots", update.callback_query.edit_message_text.call_args.args[0])

        # 2. Test Notification Toggle
        update.callback_query.data = "toggle:notifications"
        initial_state = main.state.enabled
        await main.button_handler(update, context)
        self.assertNotEqual(main.state.enabled, initial_state)
        update.callback_query.edit_message_reply_markup.assert_called()

        # 3. Test "All" Command Trigger
        update.callback_query.data = "all:photo_all"
        await main.button_handler(update, context)
        mock_cmd_photo_all.assert_called_with(update, context)

        # 4. Test Single Camera Command Trigger
        update.callback_query.data = "cmd:photo:garage"
        await main.button_handler(update, context)
        mock_cmd_photo.assert_called_with(update, context)
        self.assertEqual(context.args, ["garage"])
        
    @patch("main.fetch_event_media")
    @patch("main.fetch_event_details")
    @patch("main.fetch_recording_clip")
    @patch("asyncio.sleep", return_value=None)
    async def test_fetch_video_data_robust_fallbacks(self, mock_sleep, mock_recording, mock_details, mock_media):
        """Test fetch_video_data_robust fallback chain."""
        client = MagicMock()
        
        # Scenario 1: Pre-generated clip success
        mock_media.return_value = b"event_clip"
        data = await main.fetch_video_data_robust(client, "cam1", "evt1")
        self.assertEqual(data, b"event_clip")
        mock_media.assert_called()
        
        # Scenario 2: Pre-generated clip fails, precise recording success
        mock_media.return_value = None
        mock_details.return_value = {"start_time": 100, "end_time": 130}
        mock_recording.return_value = b"precise_clip"
        data = await main.fetch_video_data_robust(client, "cam1", "evt1")
        self.assertEqual(data, b"precise_clip")
        mock_recording.assert_any_call(client, "cam1", 100, 130)
        
        # Scenario 3: Everything fails, rough recording fallback
        mock_media.return_value = None
        mock_details.return_value = None
        mock_recording.return_value = b"rough_clip"
        data = await main.fetch_video_data_robust(client, "cam1", "evt1")
        self.assertEqual(data, b"rough_clip")

    @patch("main.fetch_recent_events")
    @patch("main.fetch_video_data_robust")
    async def test_cmd_video_last_success(self, mock_robust, mock_fetch_events):
        """Test cmd_video_last with successful fetch."""
        # Setup context
        update = MagicMock()
        update.effective_chat.send_message = AsyncMock()
        update.effective_chat.send_video = AsyncMock()
        
        context = MagicMock()
        context.args = ["garage"]
        context.bot_data = {"http_client": MagicMock()}

        # Mocks
        mock_fetch_events.return_value = [{
            "id": "evt_last",
            "camera": "garage",
            "label": "person",
            "start_time": 1000,
            "end_time": 1030,
            "zones": [],
            "thumbnail": "thumb"
        }]
        mock_robust.return_value = b"video_bytes"

        with patch("main.TELEGRAM_CHAT_ID", 12345):
             update.effective_chat.id = 12345
             await main.cmd_video_last(update, context)

        # Verify
        mock_fetch_events.assert_called_with(context.bot_data["http_client"], "garage", limit=5)
        update.effective_chat.send_message.assert_called()
        update.effective_chat.send_video.assert_called_once()

    @patch("main._http_auth")
    async def test_fetch_review_items(self, mock_auth):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": "rev_1", "camera": "cam1"}]
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        items = await main.fetch_review_items(mock_client, after_ts=0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "rev_1")

        args, kwargs = mock_client.get.call_args
        self.assertIn("/api/review", args[0])
        self.assertEqual(kwargs["params"]["after"], 0)

    async def test_fetch_review_items_error_returns_empty(self):
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Network error")

        items = await main.fetch_review_items(mock_client, after_ts=0)
        self.assertEqual(items, [])

    async def test_fetch_review_items_empty_response(self):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp

        items = await main.fetch_review_items(mock_client, after_ts=100)
        self.assertEqual(items, [])

class TestPollingTick(unittest.IsolatedAsyncioTestCase):
    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_two_ticks_hold_then_finalize_and_send(self, mock_fetch_reviews, mock_send):
        """End-to-end across two poll ticks: a review item arrives and is
        held (not sent immediately); once no further activity appears for
        EVENT_MERGE_GAP seconds, the next tick finalizes and sends it."""
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        mock_send.return_value = None

        review = {
            "id": "rev1",
            "camera": "Garage",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }

        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            # Tick 1: item arrives, held.
            mock_fetch_reviews.return_value = [review]
            pending = {}
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=110)

            self.assertEqual(len(pending), 1)
            mock_send.assert_not_called()

            # Tick 2: no new activity, quiet period (45s) has elapsed.
            mock_fetch_reviews.return_value = []
            await main._polling_tick(bot, http_client, pending, last_poll_ts=last_poll_ts, now=110 + main.EVENT_MERGE_GAP + 1)

            self.assertEqual(pending, {})
            mock_send.assert_called_once()
            sent_group = mock_send.call_args.args[1]
            self.assertEqual(sent_group.camera, "Garage")

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_filters_by_monitor_config(self, mock_fetch_reviews, mock_send):
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        review = {
            "id": "rev1",
            "camera": "Backyard",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["car"], "detections": ["e1"], "zones": []},
        }
        mock_fetch_reviews.return_value = [review]

        with patch.dict(main.MONITOR_CONFIG, {"Garage": {"all"}}, clear=True):
            pending = {}
            await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=110)

        # Backyard isn't in MONITOR_CONFIG, so nothing should be tracked.
        self.assertEqual(pending, {})

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_sends_multiple_ready_groups_in_chronological_order(
        self, mock_fetch_reviews, mock_send
    ):
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        sent_order = []

        async def record_send(_bot, group, _client):
            sent_order.append(group.camera)

        mock_send.side_effect = record_send

        later_review = {
            "id": "rev_later", "camera": "Backyard", "start_time": 200, "end_time": 210,
            "data": {"objects": ["car"], "detections": ["e2"], "zones": []},
        }
        earlier_review = {
            "id": "rev_earlier", "camera": "Garage", "start_time": 100, "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }
        # Returned out of chronological order, same as a real concurrent burst.
        mock_fetch_reviews.return_value = [later_review, earlier_review]

        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            pending = {}
            # Tick 1: both groups arrive in the same batch (out of order).
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=210)
            self.assertEqual(sent_order, [])  # nothing finalized yet

            # Tick 2: quiet period elapsed for both -> finalize and send,
            # sorted by each group's own start_time, not arrival order.
            mock_fetch_reviews.return_value = []
            await main._polling_tick(
                bot, http_client, pending, last_poll_ts=last_poll_ts, now=210 + main.EVENT_MERGE_GAP + 1
            )

        self.assertEqual(sent_order, ["Garage", "Backyard"])

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_health_check_crash_does_not_abort_ready_group_send(
        self, mock_fetch_reviews, mock_send
    ):
        """Regression: a malformed /api/stats payload (e.g. Frigate restart
        returning `"service": null`) must not abort notification processing
        for the tick. Pre-fix, evaluate_stats() is called unguarded inside
        check_camera_health_and_alert, so the AttributeError it raises
        propagates out of _polling_tick and skips fetch_review_items/
        merge/send entirely for that tick — even for a group that was
        already fully ready to send."""
        bot = MagicMock()
        mock_send.return_value = None

        review = {
            "id": "rev1",
            "camera": "Garage",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }

        healthy_stats = {"service": {"uptime": 120}, "cameras": {}}
        malformed_stats = {"service": None, "cameras": {}}
        stats_to_return = [healthy_stats]

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/api/stats" in url:
                resp.json.return_value = stats_to_return[0]
            else:
                resp.json.return_value = {}
            return resp

        http_client = MagicMock()
        http_client.get = AsyncMock(side_effect=mock_get)

        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            # Tick 1: review arrives and is merged/held. Stats are well-formed.
            mock_fetch_reviews.return_value = [review]
            pending = {}
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=110)
            self.assertEqual(len(pending), 1)
            mock_send.assert_not_called()

            # Tick 2: quiet period elapsed -> group is ready. This tick's
            # /api/stats payload is malformed (a plausible transient shape
            # during Frigate startup/restart).
            stats_to_return[0] = malformed_stats
            mock_fetch_reviews.return_value = []
            await main._polling_tick(
                bot, http_client, pending, last_poll_ts=last_poll_ts, now=110 + main.EVENT_MERGE_GAP + 1
            )

        self.assertEqual(pending, {})
        mock_send.assert_called_once()

    @patch("main.check_camera_health_and_alert")
    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_health_check_runs_after_send_and_survives_earlier_crash(
        self, mock_fetch_reviews, mock_send, mock_health
    ):
        """Regression for the step-2 restructuring: the health check must
        run after the ready-group send (not before/instead of it, as it
        does pre-fix), and must still run even when notification processing
        raises earlier in the tick — proving a try/finally restructuring,
        not a naive reorder, is what decouples the two directions."""
        bot = MagicMock()
        http_client = MagicMock()
        call_order = []

        async def record_send(*args, **kwargs):
            call_order.append("send")

        async def record_health(*args, **kwargs):
            call_order.append("health")

        mock_send.side_effect = record_send
        mock_health.side_effect = record_health

        ready_review = {
            "id": "rev1", "camera": "Garage", "start_time": 100, "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }

        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            pending = {}
            mock_fetch_reviews.return_value = [ready_review]
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=110)

            call_order.clear()  # isolate the tick that actually sends
            mock_fetch_reviews.return_value = []
            await main._polling_tick(
                bot, http_client, pending, last_poll_ts=last_poll_ts, now=110 + main.EVENT_MERGE_GAP + 1
            )

            # Health check must run only after the ready group is sent.
            self.assertEqual(call_order, ["send", "health"])

            # The health check must still run even if notification
            # processing raises earlier in the tick (malformed review data).
            call_order.clear()
            malformed_review = {"id": "rev_bad", "camera": "Garage", "data": None}
            mock_fetch_reviews.return_value = [malformed_review]
            with self.assertRaises(Exception):
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=200)

        self.assertIn("health", call_order)

    @patch("main.check_camera_health_and_alert")
    @patch("main.fetch_review_items")
    @patch("asyncio.sleep")
    async def test_polling_loop_runs_health_check_even_when_notifications_disabled(
        self, mock_sleep, mock_fetch_reviews, mock_health
    ):
        """Regression: /disable must not silently disable camera-health
        alerts. Today `_polling_tick` (and therefore
        check_camera_health_and_alert) is only invoked from inside
        `if state.enabled:` in polling_loop, so disabling notifications
        also stops health monitoring for as long as it's disabled."""
        bot = MagicMock()
        http_client = MagicMock()
        mock_fetch_reviews.return_value = []

        class _StopLoop(Exception):
            pass

        mock_sleep.side_effect = _StopLoop()

        original_enabled = main.state._enabled
        main.state._enabled = False
        try:
            with self.assertRaises(_StopLoop):
                await main.polling_loop(bot, http_client)
        finally:
            main.state._enabled = original_enabled

        mock_health.assert_called_once()
        mock_fetch_reviews.assert_not_called()

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_suppresses_night_alert_camera_outside_window(
        self, mock_fetch_reviews, mock_send
    ):
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        review = {
            "id": "rev1",
            "camera": "indoor_hallway",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }
        mock_fetch_reviews.return_value = [review]

        with _night_alert_config({"indoor_hallway"}, dt_time(22, 0), dt_time(6, 0)):
            now = _epoch_at(12, 0)  # outside the 22:00-06:00 window

            with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
                pending = {}
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=now)

        # Suppressed before grouping: never held, never sent.
        self.assertEqual(pending, {})
        mock_send.assert_not_called()

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_allows_night_alert_camera_inside_window(
        self, mock_fetch_reviews, mock_send
    ):
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        review = {
            "id": "rev1",
            "camera": "indoor_hallway",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }
        mock_fetch_reviews.return_value = [review]

        with _night_alert_config({"indoor_hallway"}, dt_time(22, 0), dt_time(6, 0)):
            now = _epoch_at(23, 0)  # inside the 22:00-06:00 window
            # Anchor the review's start/end to `now` (a real tz-aware 2024
            # epoch) rather than the tiny 100/110 placeholders used
            # elsewhere in this file — otherwise (now - first_start) would
            # already exceed MAX_EVENT_SPAN and the group would finalize
            # immediately instead of staying held pending.
            review["start_time"] = now - 5
            review["end_time"] = now

            with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
                pending = {}
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=now)

        # Held pending (not yet sent — quiet period hasn't elapsed), but
        # not suppressed: the review reached merge_into_pending.
        self.assertEqual(len(pending), 1)

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_camera_not_in_night_alert_list_is_unaffected(
        self, mock_fetch_reviews, mock_send
    ):
        """Regression guard: a camera absent from NIGHT_ALERT_CAMERAS must be
        processed regardless of `now`, even at a `now` that would suppress a
        listed camera."""
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        review = {
            "id": "rev1",
            "camera": "Garage",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }
        mock_fetch_reviews.return_value = [review]

        with _night_alert_config({"indoor_hallway"}, dt_time(22, 0), dt_time(6, 0)):
            now = _epoch_at(12, 0)  # outside indoor_hallway's window
            # See comment in test_polling_tick_allows_night_alert_camera_inside_window:
            # anchor start/end to `now` so the group doesn't instantly exceed
            # MAX_EVENT_SPAN and finalize before this assertion runs.
            review["start_time"] = now - 5
            review["end_time"] = now

            with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
                pending = {}
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=now)

        # Garage isn't in NIGHT_ALERT_CAMERAS, so it must still be held.
        self.assertEqual(len(pending), 1)

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_refetches_and_merges_in_place_updated_pending_review(
        self, mock_fetch_reviews, mock_send
    ):
        """Regression for the 21:24 Garage bug: Frigate appends a second
        detection ('vhnxf1') in-place to an existing review item ('sw6351')
        whose start_time (100) is earlier than last_poll_ts (105). While
        'sw6351' is in pending, _polling_tick must look back to its
        first_start (100), merge the new detection and extended end_time,
        refresh last_seen_at on the update, and then finalize after the
        quiet debounce window without starving when the closed review is
        re-polled unchanged."""
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        mock_send.return_value = None

        r1_initial = {
            "id": "sw6351",
            "camera": "Garage",
            "start_time": 100,
            "end_time": 104,
            "data": {"objects": ["person"], "detections": ["b372u1"], "zones": ["notification_zone"]},
        }
        r1_updated = {
            "id": "sw6351",
            "camera": "Garage",
            "start_time": 100,
            "end_time": 128,
            "data": {"objects": ["person"], "detections": ["b372u1", "vhnxf1"], "zones": ["notification_zone"]},
        }

        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            pending = {}

            # Tick 1 (now=105, last_poll_ts=90): initial review arrives and is held in pending.
            mock_fetch_reviews.return_value = [r1_initial]
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=90, now=105)
            mock_fetch_reviews.assert_called_with(http_client, 90)
            self.assertEqual(last_poll_ts, 105)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending["sw6351"].event_ids, {"b372u1"})
            mock_send.assert_not_called()

            # Tick 2 (now=135, last_poll_ts=105): Frigate updated sw6351 in-place with vhnxf1.
            # _polling_tick must query with after_ts=100 (min(105, 100)) so sw6351 is re-fetched.
            mock_fetch_reviews.side_effect = lambda _client, after_ts: [r1_updated] if after_ts <= 100 else []
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=last_poll_ts, now=135)
            mock_fetch_reviews.assert_called_with(http_client, 100)
            self.assertEqual(last_poll_ts, 135)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending["sw6351"].event_ids, {"b372u1", "vhnxf1"})
            self.assertEqual(pending["sw6351"].last_activity_end, 128)
            self.assertEqual(pending["sw6351"].last_seen_at, 135)
            mock_send.assert_not_called()

            # Tick 3 (now=181, last_poll_ts=135): 46s (>= 45s EVENT_MERGE_GAP) after the update at 135.
            # Even though the lookback query still returns the unchanged closed r1_updated,
            # last_seen_at must NOT be reset to 181, so the group finalizes and sends both events.
            last_poll_ts = await main._polling_tick(bot, http_client, pending, last_poll_ts=last_poll_ts, now=181)
            self.assertEqual(pending, {})
            mock_send.assert_called_once()
            sent_group = mock_send.call_args.args[1]
            self.assertEqual(sent_group.camera, "Garage")
            self.assertEqual(sent_group.review_ids, ["sw6351"])
            self.assertEqual(sent_group.event_ids, {"b372u1", "vhnxf1"})
            self.assertEqual(sent_group.last_activity_end, 128)

    @patch("main.send_grouped_notification")
    @patch("main.fetch_review_items")
    async def test_polling_tick_lookback_ignores_already_sent_reviews_from_other_cameras(
        self, mock_fetch_reviews, mock_send
    ):
        """When pending holds a group starting at t=100 and last_poll_ts is
        130, the lookback query (after_ts=100) may also return an older
        review (t=115 < last_poll_ts) that was already finalized and is no
        longer in pending. _polling_tick must ignore that non-pending older
        review rather than re-queuing it."""
        bot = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=MagicMock(
            status_code=200,
            json=MagicMock(return_value={"service": {"uptime": 0}, "cameras": {}}),
        ))
        mock_send.return_value = None

        pending_review = {
            "id": "rev_pending",
            "camera": "Garage",
            "start_time": 100,
            "end_time": 110,
            "data": {"objects": ["person"], "detections": ["e1"], "zones": []},
        }
        already_sent_review = {
            "id": "rev_already_sent",
            "camera": "Backyard",
            "start_time": 115,
            "end_time": 120,
            "data": {"objects": ["car"], "detections": ["e_old"], "zones": []},
        }

        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            pending = {
                "rev_pending": grouping.PendingGroup(
                    camera="Garage",
                    labels={"person"},
                    review_ids=["rev_pending"],
                    event_ids={"e1"},
                    first_start=100,
                    last_activity_end=110,
                    last_seen_at=110,
                )
            }
            mock_fetch_reviews.return_value = [pending_review, already_sent_review]

            await main._polling_tick(bot, http_client, pending, last_poll_ts=130, now=135)

            # rev_already_sent has start_time (115) < last_poll_ts (130) and is not in pending,
            # so it must NOT be added to pending.
            self.assertEqual(list(pending.keys()), ["rev_pending"])
            mock_send.assert_not_called()


class TestMatchesMonitorConfig(unittest.TestCase):
    def test_matches_monitor_config(self):
        # 1. Empty config
        with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
            self.assertTrue(main.matches_monitor_config("any", []))

        # 2. Camera not in config
        with patch.dict(main.MONITOR_CONFIG, {"front": {"all"}}, clear=True):
            self.assertFalse(main.matches_monitor_config("back", []))

        # 3. Camera in config, zone is 'all'
        with patch.dict(main.MONITOR_CONFIG, {"front": {"all"}}, clear=True):
            self.assertTrue(main.matches_monitor_config("front", ["driveway"]))

        # 4. Camera in config, matching zone
        with patch.dict(main.MONITOR_CONFIG, {"front": {"driveway", "porch"}}, clear=True):
            self.assertTrue(main.matches_monitor_config("front", ["driveway"]))

        # 5. Camera in config, multiple event zones, one matching
        with patch.dict(main.MONITOR_CONFIG, {"front": {"driveway", "porch"}}, clear=True):
            self.assertTrue(main.matches_monitor_config("front", ["street", "driveway"]))

        # 6. Camera in config, no matching zones
        with patch.dict(main.MONITOR_CONFIG, {"front": {"driveway", "porch"}}, clear=True):
            self.assertFalse(main.matches_monitor_config("front", ["street"]))

        # 7. Missing camera field (defaults to "" at the call site)
        with patch.dict(main.MONITOR_CONFIG, {"": {"all"}}, clear=True):
            self.assertTrue(main.matches_monitor_config("", ["any"]))

        # 8. Missing zones field (defaults to [])
        with patch.dict(main.MONITOR_CONFIG, {"front": {"driveway"}}, clear=True):
            self.assertFalse(main.matches_monitor_config("front", []))


class TestMatchesNightAlertSchedule(unittest.TestCase):

    def test_camera_not_in_night_alert_cameras_is_always_allowed(self):
        with _night_alert_config({"back_yard"}, dt_time(22, 0), dt_time(6, 0)):
            # now is clearly outside any night window (midday); unlisted
            # camera must still be allowed — the feature is a true no-op
            # for cameras not in NIGHT_ALERT_CAMERAS.
            now = _epoch_at(12, 0)
            self.assertTrue(main.matches_night_alert_schedule("front_door", now))

    def test_listed_camera_allowed_inside_window(self):
        with _night_alert_config({"indoor_hallway"}, dt_time(22, 0), dt_time(6, 0)):
            now = _epoch_at(23, 0)  # inside the 22:00-06:00 window
            self.assertTrue(main.matches_night_alert_schedule("indoor_hallway", now))

    def test_listed_camera_blocked_outside_window(self):
        with _night_alert_config({"indoor_hallway"}, dt_time(22, 0), dt_time(6, 0)):
            now = _epoch_at(12, 0)  # outside the 22:00-06:00 window
            self.assertFalse(main.matches_night_alert_schedule("indoor_hallway", now))

    def test_degenerate_equal_start_end_always_false_for_listed_camera(self):
        """NIGHT_ALERT_START == NIGHT_ALERT_END is a degenerate empty
        interval and must mean 'never notify' for listed cameras, for any
        `now` — this is the documented (not just assumed) fallout of
        in_night_window's half-open-interval semantics."""
        with _night_alert_config({"indoor_hallway"}, dt_time(8, 0), dt_time(8, 0)):
            for hour, minute in [(8, 0), (12, 0), (23, 59), (0, 0)]:
                now = _epoch_at(hour, minute)
                self.assertFalse(main.matches_night_alert_schedule("indoor_hallway", now))


class TestCameraHealthIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if hasattr(main, "camera_health_monitor"):
            main.camera_health_monitor.states.clear()

    @patch("main.fetch_review_items", return_value=[])
    async def test_polling_tick_dispatches_camera_offline_alert(self, mock_reviews):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        stats_offline = {
            "service": {"uptime": 120},
            "cameras": {
                "FrontDoor": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"}
            }
        }
        logs_data = {
            "lines": [
                "2026-09-18 16:54:58  [ERROR] [ffmpeg.FrontDoor.record] Error during demuxing: Connection timed out"
            ]
        }

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/api/stats" in url:
                resp.json.return_value = stats_offline
            elif "/api/logs/frigate" in url:
                resp.json.return_value = logs_data
            else:
                resp.json.return_value = {}
            return resp

        http_client = MagicMock()
        http_client.get = AsyncMock(side_effect=mock_get)

        pending = {}
        # Tick 1 at t=100 (first failure, debounce pending)
        await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=100.0)
        bot.send_message.assert_not_called()

        # Tick 2 at t=165 (65s elapsed > 60s debounce)
        await main._polling_tick(bot, http_client, pending, last_poll_ts=100.0, now=165.0)
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        self.assertIn("FrontDoor is OFFLINE", call_kwargs["text"])
        self.assertIn("Error during demuxing: Connection timed out", call_kwargs["text"])

    @patch("main.fetch_review_items", return_value=[])
    async def test_polling_tick_dispatches_camera_recovery_alert(self, mock_reviews):
        bot = MagicMock()
        bot.send_message = AsyncMock()

        stats_offline = {
            "service": {"uptime": 120},
            "cameras": {
                "FrontDoor": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"}
            }
        }
        stats_online = {
            "service": {"uptime": 180},
            "cameras": {
                "FrontDoor": {"camera_fps": 5.0, "expected_fps": 5.0, "connection_quality": "good"}
            }
        }

        current_stats = [stats_offline]

        async def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "/api/stats" in url:
                resp.json.return_value = current_stats[0]
            else:
                resp.json.return_value = {"lines": []}
            return resp

        http_client = MagicMock()
        http_client.get = AsyncMock(side_effect=mock_get)

        pending = {}
        # Trigger initial offline alert
        await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=100.0)
        await main._polling_tick(bot, http_client, pending, last_poll_ts=100.0, now=165.0)
        self.assertEqual(bot.send_message.call_count, 1)

        # Now camera recovers at t=200.0
        current_stats[0] = stats_online
        await main._polling_tick(bot, http_client, pending, last_poll_ts=165.0, now=200.0)
        self.assertEqual(bot.send_message.call_count, 2)
        recovery_text = bot.send_message.call_args.kwargs["text"]
        self.assertIn("FrontDoor is BACK ONLINE", recovery_text)
        self.assertIn("Downtime:", recovery_text)

    async def test_cmd_status_displays_camera_health(self):
        update = MagicMock()
        update.effective_chat.id = main.TELEGRAM_CHAT_ID
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()

        # Populate camera health state
        main.camera_health_monitor.update_camera(
            camera="FrontDoor", is_failing=False, current_fps=5.0, expected_fps=5.0, now=100.0
        )
        main.camera_health_monitor.update_camera(
            camera="Driveway", is_failing=True, current_fps=0.0, expected_fps=5.0, now=100.0
        )
        main.camera_health_monitor.update_camera(
            camera="Driveway", is_failing=True, current_fps=0.0, expected_fps=5.0, now=165.0
        )

        await main.cmd_status(update, context)
        update.effective_chat.send_message.assert_called_once()
        text = update.effective_chat.send_message.call_args.kwargs.get("text") or update.effective_chat.send_message.call_args.args[0]
        self.assertIn("Camera Health", text)
        self.assertIn("FrontDoor", text)
        self.assertIn("Driveway", text)

    @patch("main.fetch_camera_list")
    async def test_cmd_cameras_displays_camera_health(self, mock_fetch_cameras):
        mock_fetch_cameras.return_value = ["FrontDoor", "Driveway", "Backyard"]
        update = MagicMock()
        update.effective_chat.id = main.TELEGRAM_CHAT_ID
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()
        context.bot_data = {"http_client": AsyncMock()}

        # FrontDoor healthy, Driveway failing, Backyard has no tracked
        # health state at all (never seen in an /api/stats payload yet).
        main.camera_health_monitor.update_camera(
            camera="FrontDoor", is_failing=False, current_fps=5.0, expected_fps=5.0, now=100.0
        )
        main.camera_health_monitor.update_camera(
            camera="Driveway", is_failing=True, current_fps=0.0, expected_fps=5.0, now=100.0
        )
        main.camera_health_monitor.update_camera(
            camera="Driveway", is_failing=True, current_fps=0.0, expected_fps=5.0, now=165.0
        )
        self.assertNotIn("Backyard", main.camera_health_monitor.states)

        await main.cmd_cameras(update, context)
        update.effective_chat.send_message.assert_called_once()
        text = update.effective_chat.send_message.call_args.kwargs.get("text") or update.effective_chat.send_message.call_args.args[0]
        self.assertIn("FrontDoor", text)
        self.assertIn("Driveway", text)
        self.assertIn("Online", text)
        self.assertIn("Offline", text)
        # A camera absent from camera_health_monitor.states (cstate is None)
        # must still be reported Online, not omitted or mis-rendered — this
        # pins the cstate-is-None branch ahead of collapsing the tautological
        # if/elif/else into a single ternary.
        backyard_line = next(line for line in text.splitlines() if "Backyard" in line)
        self.assertIn("🟢 Online", backyard_line)

    async def test_cmd_status_displays_night_alert_cameras_when_configured(self):
        update = MagicMock()
        update.effective_chat.id = main.TELEGRAM_CHAT_ID
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()

        with _night_alert_config({"indoor_hallway"}, dt_time(22, 0), dt_time(6, 0)):
            await main.cmd_status(update, context)

        update.effective_chat.send_message.assert_called_once()
        text = update.effective_chat.send_message.call_args.kwargs.get("text") or update.effective_chat.send_message.call_args.args[0]
        self.assertIn("Night Alert Cameras", text)
        self.assertIn("indoor_hallway", text)
        self.assertIn("22:00", text)
        self.assertIn("06:00", text)

    async def test_cmd_status_displays_none_configured_when_night_alert_cameras_empty(self):
        update = MagicMock()
        update.effective_chat.id = main.TELEGRAM_CHAT_ID
        update.effective_chat.send_message = AsyncMock()
        context = MagicMock()

        with _night_alert_config(set()):
            await main.cmd_status(update, context)

        update.effective_chat.send_message.assert_called_once()
        text = update.effective_chat.send_message.call_args.kwargs.get("text") or update.effective_chat.send_message.call_args.args[0]
        self.assertIn("Night Alert Cameras", text)
        self.assertIn("None configured", text)
        # Window suffix must not render when the feature is off (empty camera set) —
        # otherwise "None configured (22:00-06:00)" misleadingly implies an active window.
        self.assertNotIn("22:00", text)
        self.assertNotIn("06:00", text)


def _stats_with_cache(used, total=2048.0, uptime=120, cameras=None):
    return {
        "service": {
            "uptime": uptime,
            "storage": {"/tmp/cache": {"total": total, "used": used, "free": total - used, "mount_type": "tmpfs"}},
        },
        "cameras": cameras or {},
    }


def _stats_http_client(current_stats):
    """http_client whose /api/stats returns current_stats[0] (mutable holder)."""
    async def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "/api/stats" in url:
            resp.json.return_value = current_stats[0]
        else:
            resp.json.return_value = {"lines": []}
        return resp

    http_client = MagicMock()
    http_client.get = AsyncMock(side_effect=mock_get)
    return http_client


class TestCacheHealthIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._reset_monitors()
        self.addCleanup(self._reset_monitors)

    @staticmethod
    def _reset_monitors():
        main.camera_health_monitor.states.clear()
        main.cache_health_monitor.states.clear()
        main.cache_health_monitor.last_pct = None
        main.cache_health_monitor.last_used = None
        main.cache_health_monitor.last_total = None

    def test_cache_threshold_default_and_monitor_type(self):
        import camera_health
        self.assertTrue(1 <= main.HEALTH_CACHE_THRESHOLD_PCT <= 100)
        self.assertIsInstance(main.cache_health_monitor, camera_health.CacheStorageMonitor)
        self.assertIsNot(main.cache_health_monitor, main.camera_health_monitor)

    async def test_full_cache_sends_alert_then_recovery(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        current = [_stats_with_cache(2000.0)]
        http_client = _stats_http_client(current)

        await main.check_camera_health_and_alert(bot, http_client, now=100.0)
        bot.send_message.assert_not_called()  # debounce

        await main.check_camera_health_and_alert(bot, http_client, now=161.0)
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], main.TELEGRAM_CHAT_ID)
        self.assertEqual(kwargs["parse_mode"], main.ParseMode.HTML)
        self.assertIn("/tmp/cache", kwargs["text"])
        self.assertIn("98%", kwargs["text"])
        self.assertNotIn("fps", kwargs["text"].lower())

        # Cache drains after a Frigate restart.
        current[0] = _stats_with_cache(32.1)
        await main.check_camera_health_and_alert(bot, http_client, now=200.0)
        self.assertEqual(bot.send_message.call_count, 2)
        recovery = bot.send_message.call_args.kwargs["text"]
        self.assertIn("cache", recovery.lower())
        self.assertIn("recovered", recovery.lower())

    @patch("main.fetch_review_items", return_value=[])
    async def test_polling_tick_dispatches_cache_alert(self, mock_reviews):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        http_client = _stats_http_client([_stats_with_cache(2000.0)])

        pending = {}
        await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=100.0)
        await main._polling_tick(bot, http_client, pending, last_poll_ts=100.0, now=165.0)
        texts = [c.kwargs["text"] for c in bot.send_message.call_args_list]
        self.assertEqual(len([t for t in texts if "/tmp/cache" in t]), 1)

    async def test_cache_alert_sent_even_if_camera_evaluation_raises(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        http_client = _stats_http_client([_stats_with_cache(2000.0)])

        with patch.object(main.camera_health_monitor, "evaluate_stats", side_effect=RuntimeError("boom")):
            await main.check_camera_health_and_alert(bot, http_client, now=100.0)
            await main.check_camera_health_and_alert(bot, http_client, now=161.0)

        bot.send_message.assert_called_once()
        self.assertIn("/tmp/cache", bot.send_message.call_args.kwargs["text"])

    async def test_camera_alert_sent_even_if_cache_evaluation_raises(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        cameras = {"FrontDoor": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"}}
        stats = _stats_with_cache(32.1, cameras=cameras)
        stats["service"]["storage"]["/tmp/cache"] = ["not", "a", "dict"]  # malformed
        http_client = _stats_http_client([stats])

        with patch.object(main.cache_health_monitor, "evaluate", side_effect=RuntimeError("boom")):
            await main.check_camera_health_and_alert(bot, http_client, now=100.0)
            await main.check_camera_health_and_alert(bot, http_client, now=161.0)

        bot.send_message.assert_called_once()
        self.assertIn("FrontDoor is OFFLINE", bot.send_message.call_args.kwargs["text"])

    async def test_camera_alert_sent_with_malformed_storage_payload(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        cameras = {"FrontDoor": {"camera_fps": 0.0, "expected_fps": 5.0, "connection_quality": "unusable"}}
        stats = _stats_with_cache(32.1, cameras=cameras)
        stats["service"]["storage"] = {"/tmp/cache": {"total": "big", "used": None}}
        http_client = _stats_http_client([stats])

        await main.check_camera_health_and_alert(bot, http_client, now=100.0)
        await main.check_camera_health_and_alert(bot, http_client, now=161.0)

        bot.send_message.assert_called_once()
        self.assertIn("FrontDoor is OFFLINE", bot.send_message.call_args.kwargs["text"])
        self.assertIsNone(main.cache_health_monitor.last_pct)

    async def _status_text(self):
        update = MagicMock()
        update.effective_chat.id = main.TELEGRAM_CHAT_ID
        update.effective_chat.send_message = AsyncMock()
        await main.cmd_status(update, MagicMock())
        call = update.effective_chat.send_message.call_args
        return call.kwargs.get("text") or call.args[0]

    async def test_cmd_status_shows_cache_usage_red_when_over_threshold(self):
        main.cache_health_monitor.evaluate(_stats_with_cache(2000.0), main.HEALTH_CACHE_THRESHOLD_PCT, now=100.0)
        text = await self._status_text()
        line = next(l for l in text.splitlines() if "Frigate cache" in l)
        self.assertIn("98%", line)
        self.assertIn("🔴", line)

    async def test_cmd_status_shows_cache_usage_green_when_healthy(self):
        main.cache_health_monitor.evaluate(_stats_with_cache(32.1), main.HEALTH_CACHE_THRESHOLD_PCT, now=100.0)
        text = await self._status_text()
        line = next(l for l in text.splitlines() if "Frigate cache" in l)
        self.assertIn("2%", line)
        self.assertIn("🟢", line)
        # Bulleted inside the health section, even with no camera states.
        self.assertTrue(line.startswith("• "))
        self.assertIn("Camera Health", text.split(line)[0])

    async def test_cmd_status_omits_cache_line_when_no_stats_seen(self):
        text = await self._status_text()
        self.assertNotIn("Frigate cache", text)


if __name__ == "__main__":
    unittest.main()
