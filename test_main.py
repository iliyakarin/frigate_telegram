import sys
from unittest.mock import MagicMock

# Mock dependencies that might be missing for standard unit test run
sys.modules["httpx"] = MagicMock()
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.constants"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()

import unittest
import html
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Set environment variables for main.py import
os.environ["FRIGATE_URL"] = "http://localhost:5000"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
os.environ["TELEGRAM_CHAT_ID"] = "fake"
os.environ["STATE_FILE"] = "state.json"

import main

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
    @patch("main.fetch_event_details")
    @patch("main.fetch_event_media")
    @patch("main.fetch_camera_snapshot")
    async def test_send_event_notification_refetches(self, mock_snap, mock_media, mock_fetch_details):
        bot = MagicMock()
        bot.send_animation = AsyncMock()
        http_client = MagicMock()
        event = {"id": "123", "camera": "cam"}

        mock_fetch_details.return_value = {"id": "123", "camera": "cam", "sub_label": "Found", "start_time": 1672531200}
        mock_media.return_value = b"gif_data"
        mock_snap.return_value = b"snap_data"

        # Set MEDIA_WAIT_TIMEOUT to 0 for faster test
        original_timeout = main.MEDIA_WAIT_TIMEOUT
        main.MEDIA_WAIT_TIMEOUT = 0
        try:
            await main.send_event_notification(bot, event, http_client)
        finally:
            main.MEDIA_WAIT_TIMEOUT = original_timeout

        mock_fetch_details.assert_called_once_with(http_client, "123")
        # Verify that Found is in the caption sent to telegram
        call_args = bot.send_animation.call_args
        self.assertIn("Found", call_args.kwargs["caption"])

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
        mock_media.assert_called_with(context.bot_data["http_client"], "evt_123", "clip")
        
        # Ensure we sent a video via effective_chat
        update.effective_chat.send_video.assert_called_once()

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
        update.callback_query = AsyncMock()
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
        update.effective_message.reply_text = AsyncMock()
        update.effective_message.reply_video = AsyncMock()
        
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
        update.effective_message.reply_text.assert_called()
        update.effective_message.reply_video.assert_called_once()
    
if __name__ == "__main__":
    unittest.main()
