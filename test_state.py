import sys
from unittest.mock import MagicMock

# Mock dependencies that might be missing for standard unit test run
sys.modules["httpx"] = MagicMock()
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.constants"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

import unittest
import json
import logging
from pathlib import Path
from unittest.mock import patch
import main

class TestNotificationState(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_path = Path("test_state.json")
        if self.test_path.exists():
            self.test_path.unlink()

    def tearDown(self):
        if self.test_path.exists():
            self.test_path.unlink()

    def test_init_no_file(self):
        # Should default to enabled: True if file doesn't exist
        state = main.NotificationState(self.test_path)
        self.assertTrue(state.enabled)

    def test_load_enabled_true(self):
        self.test_path.write_text(json.dumps({"enabled": True}))
        state = main.NotificationState(self.test_path)
        self.assertTrue(state.enabled)

    def test_load_enabled_false(self):
        self.test_path.write_text(json.dumps({"enabled": False}))
        state = main.NotificationState(self.test_path)
        self.assertFalse(state.enabled)

    def test_load_invalid_json(self):
        self.test_path.write_text("invalid json")
        # Should default to enabled: True on JSON error
        state = main.NotificationState(self.test_path)
        self.assertTrue(state.enabled)

    def test_load_io_error(self):
        # Trigger an exception during read_text
        with patch.object(Path, "read_text", side_effect=OSError("Read error")):
            # Create the file so exists() returns True
            self.test_path.touch()
            state = main.NotificationState(self.test_path)
            self.assertTrue(state.enabled)

    async def test_save_io_error(self):
        # Patch write_text to raise an exception during _save
        state = main.NotificationState(self.test_path)
        with patch.object(Path, "write_text", side_effect=OSError("Write error")):
            with self.assertLogs("frigate-telegram", level="WARNING") as cm:
                await state._save()
                self.assertTrue(any("Could not persist state file" in output for output in cm.output))
        # Ensure the test didn't crash and we reached here
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
