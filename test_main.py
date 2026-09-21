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
from datetime import datetime, time as dt_time
from unittest.mock import AsyncMock, patch

# Set environment variables for main.py import
os.environ["FRIGATE_URL"] = "http://localhost:5000"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
os.environ["TELEGRAM_CHAT_ID"] = "fake"
os.environ["STATE_FILE"] = "state.json"

import main
import grouping

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
    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_sends_video_with_union_bounds(
        self, mock_details, mock_clip, mock_snap
    ):
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
        mock_clip.return_value = b"clip_bytes"
        mock_snap.return_value = b"snap_bytes"

        await main.send_grouped_notification(bot, group, http_client)

        # Clip fetched over the union of constituent event bounds padded by
        # CLIP_PADDING_SECONDS on each side, not the exact/review's own bounds.
        mock_clip.assert_called_once_with(
            http_client, "Garage", 100 - main.CLIP_PADDING_SECONDS, 200 + main.CLIP_PADDING_SECONDS
        )
        bot.send_video.assert_called_once()
        call_kwargs = bot.send_video.call_args.kwargs
        self.assertEqual(call_kwargs["video"], b"clip_bytes")
        self.assertIn("Found", call_kwargs["caption"])
        self.assertIn("driveway, porch", call_kwargs["caption"])

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_clamps_padding_at_zero(self, mock_details, mock_clip, mock_snap):
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

    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_splits_into_two_clips_when_over_limit(
        self, mock_details, mock_clip, mock_snap
    ):
        bot = MagicMock()
        bot.send_video = AsyncMock()
        http_client = MagicMock()

        group = grouping.PendingGroup(
            camera='Backyard', labels={'person'}, review_ids=['rev1'], event_ids={'e1'},
            first_start=100, last_activity_end=200, last_seen_at=200,
        )
        mock_details.return_value = {'id': 'e1', 'label': 'person', 'zones': [], 'start_time': 100, 'end_time': 200}

        def clip_side_effect(_client, camera, start, end):
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

    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_handles_partial_split_success(
        self, mock_details, mock_clip, mock_snap
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

        def clip_side_effect(_client, camera, start, end):
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

    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_falls_back_to_photo_if_split_parts_still_too_large(
        self, mock_details, mock_clip, mock_snap
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

    @patch('main.fetch_camera_snapshot')
    @patch('main.fetch_recording_clip')
    @patch('main.fetch_event_details')
    async def test_send_grouped_notification_falls_back_to_photo_on_send_video_exception(
        self, mock_details, mock_clip, mock_snap
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

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_photo_send_failure_does_not_double_send(
        self, mock_details, mock_clip, mock_snap
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

    @patch("main.fetch_camera_snapshot")
    @patch("main.fetch_recording_clip")
    @patch("main.fetch_event_details")
    async def test_send_grouped_notification_falls_back_to_photo(self, mock_details, mock_clip, mock_snap):
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
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x" * 150  # > 100 bytes to pass size check
        mock_client.get.return_value = mock_resp

        await main.fetch_recording_clip(mock_client, "cam1", 1000, 1030)

        args, kwargs = mock_client.get.call_args
        url = args[0]
        # Should NOT have /recordings/
        self.assertNotIn("/recordings/", url)
        # main.FRIGATE_URL is http://localhost:5000 in test setup
        self.assertIn("/api/cam1/start/1000/end/1030/clip.mp4", url)

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

    @staticmethod
    def _epoch_at(hour, minute):
        """tz-aware epoch (per main._CACHED_TZ, i.e. TIMEZONE) for a given
        local hour/minute — see TestMatchesNightAlertSchedule._epoch_at."""
        return datetime(2024, 1, 1, hour, minute, tzinfo=main._CACHED_TZ).timestamp()

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

        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)
            now = self._epoch_at(12, 0)  # outside the 22:00-06:00 window

            with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
                pending = {}
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=now)
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

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

        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)
            now = self._epoch_at(23, 0)  # inside the 22:00-06:00 window

            with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
                pending = {}
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=now)
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

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

        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)
            now = self._epoch_at(12, 0)  # outside indoor_hallway's window

            with patch.dict(main.MONITOR_CONFIG, {}, clear=True):
                pending = {}
                await main._polling_tick(bot, http_client, pending, last_poll_ts=0, now=now)
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

        # Garage isn't in NIGHT_ALERT_CAMERAS, so it must still be held.
        self.assertEqual(len(pending), 1)


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
    @staticmethod
    def _epoch_at(hour, minute):
        """Build a tz-aware epoch (per main._CACHED_TZ, i.e. TIMEZONE) for a
        given local hour/minute — never a bare naive datetime.timestamp(),
        which would silently use the test-runner machine's local timezone."""
        return datetime(2024, 1, 1, hour, minute, tzinfo=main._CACHED_TZ).timestamp()

    def test_camera_not_in_night_alert_cameras_is_always_allowed(self):
        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"back_yard"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)
            # now is clearly outside any night window (midday); unlisted
            # camera must still be allowed — the feature is a true no-op
            # for cameras not in NIGHT_ALERT_CAMERAS.
            now = self._epoch_at(12, 0)
            self.assertTrue(main.matches_night_alert_schedule("front_door", now))
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

    def test_listed_camera_allowed_inside_window(self):
        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)
            now = self._epoch_at(23, 0)  # inside the 22:00-06:00 window
            self.assertTrue(main.matches_night_alert_schedule("indoor_hallway", now))
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

    def test_listed_camera_blocked_outside_window(self):
        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)
            now = self._epoch_at(12, 0)  # outside the 22:00-06:00 window
            self.assertFalse(main.matches_night_alert_schedule("indoor_hallway", now))
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

    def test_degenerate_equal_start_end_always_false_for_listed_camera(self):
        """NIGHT_ALERT_START == NIGHT_ALERT_END is a degenerate empty
        interval and must mean 'never notify' for listed cameras, for any
        `now` — this is the documented (not just assumed) fallout of
        in_night_window's half-open-interval semantics."""
        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(8, 0)
            main.NIGHT_ALERT_END = dt_time(8, 0)
            for hour, minute in [(8, 0), (12, 0), (23, 59), (0, 0)]:
                now = self._epoch_at(hour, minute)
                self.assertFalse(main.matches_night_alert_schedule("indoor_hallway", now))
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end


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

        original_cameras = main.NIGHT_ALERT_CAMERAS
        original_start = main.NIGHT_ALERT_START
        original_end = main.NIGHT_ALERT_END
        try:
            main.NIGHT_ALERT_CAMERAS = {"indoor_hallway"}
            main.NIGHT_ALERT_START = dt_time(22, 0)
            main.NIGHT_ALERT_END = dt_time(6, 0)

            await main.cmd_status(update, context)
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras
            main.NIGHT_ALERT_START = original_start
            main.NIGHT_ALERT_END = original_end

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

        original_cameras = main.NIGHT_ALERT_CAMERAS
        try:
            main.NIGHT_ALERT_CAMERAS = set()
            await main.cmd_status(update, context)
        finally:
            main.NIGHT_ALERT_CAMERAS = original_cameras

        update.effective_chat.send_message.assert_called_once()
        text = update.effective_chat.send_message.call_args.kwargs.get("text") or update.effective_chat.send_message.call_args.args[0]
        self.assertIn("Night Alert Cameras", text)
        self.assertIn("None configured", text)


if __name__ == "__main__":
    unittest.main()
