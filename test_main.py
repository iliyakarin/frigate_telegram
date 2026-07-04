import sys
from unittest.mock import MagicMock

# Mock dependencies that might be missing for standard unit test run
sys.modules["httpx"] = MagicMock()
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.constants"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

import unittest
import os
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

if __name__ == "__main__":
    unittest.main()
