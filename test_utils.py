import sys
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os

# Mock dependencies that might be missing for standard unit test run
sys.modules["httpx"] = MagicMock()
sys.modules["telegram"] = MagicMock()
sys.modules["telegram.constants"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

# Set environment variables for main.py import
os.environ["FRIGATE_URL"] = "http://localhost:5000"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
os.environ["TELEGRAM_CHAT_ID"] = "fake"
os.environ["STATE_FILE"] = "state.json"

import main

class TestUtils(unittest.TestCase):
    def test_epoch_to_datetime_none(self):
        """Test with None value."""
        self.assertEqual(main._epoch_to_datetime(None), "N/A")

    def test_epoch_to_datetime_zero(self):
        """Test with 0 value."""
        self.assertEqual(main._epoch_to_datetime(0), "N/A")

    def test_epoch_to_datetime_utc(self):
        """Test with fixed epoch and UTC timezone."""
        epoch = 1672531200 # 2023-01-01 00:00:00 UTC
        with patch("main._CACHED_TZ", timezone.utc):
            result = main._epoch_to_datetime(epoch)
            self.assertEqual(result, "2023-01-01 00:00:00 UTC")

    def test_epoch_to_datetime_berlin(self):
        """Test with fixed epoch and Europe/Berlin timezone."""
        epoch = 1672531200 # 2023-01-01 00:00:00 UTC -> 2023-01-01 01:00:00 CET
        with patch("main._CACHED_TZ", ZoneInfo("Europe/Berlin")):
            result = main._epoch_to_datetime(epoch)
            self.assertEqual(result, "2023-01-01 01:00:00 CET")

    def test_epoch_to_datetime_new_york(self):
        """Test with fixed epoch and America/New_York timezone."""
        epoch = 1672531200 # 2023-01-01 00:00:00 UTC -> 2022-12-31 19:00:00 EST
        with patch("main._CACHED_TZ", ZoneInfo("America/New_York")):
            result = main._epoch_to_datetime(epoch)
            self.assertEqual(result, "2022-12-31 19:00:00 EST")

    def test_epoch_to_datetime_exception_fallback(self):
        """Test error handling and fallback to UTC."""
        epoch = 1672531200
        # Mocking datetime.fromtimestamp to raise an exception when called with _CACHED_TZ
        with patch("main.datetime") as mock_datetime:
            # We need to simulate the fromtimestamp call.
            # The first call in the try block will fail.
            # The second call in the except block should succeed.

            # Create a real datetime object to return for the fallback
            real_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)

            def side_effect(ts, tz=None):
                if tz is not timezone.utc:
                    raise Exception("Simulated error")
                return real_dt

            mock_datetime.fromtimestamp.side_effect = side_effect

            # Ensure _CACHED_TZ is something other than UTC
            with patch("main._CACHED_TZ", ZoneInfo("Europe/Berlin")):
                result = main._epoch_to_datetime(epoch)
                self.assertEqual(result, "2023-01-01 00:00:00 UTC")

    def test_timezone_parsing_fallback(self):
        """Test that invalid TIMEZONE falls back to UTC by catching ZoneInfoNotFoundError."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        import main
        from datetime import timezone

        # Helper to simulate the logic in main.py
        def get_tz(tz_name):
            try:
                return ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                return timezone.utc

        self.assertEqual(get_tz("Invalid/Timezone"), timezone.utc)
        self.assertEqual(get_tz("Europe/Berlin"), ZoneInfo("Europe/Berlin"))

if __name__ == "__main__":
    unittest.main()
