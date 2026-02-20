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

if __name__ == "__main__":
    unittest.main()
