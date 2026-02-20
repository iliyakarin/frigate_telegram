import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Set required environment variables BEFORE importing main
os.environ["FRIGATE_URL"] = "http://localhost:5000"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token"
os.environ["TELEGRAM_CHAT_ID"] = "12345"
os.environ["STATE_FILE"] = "test_state.json"

import main

class TestSecurity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reset state for each test
        await main.state.enable()
        if os.path.exists("test_state.json"):
            os.remove("test_state.json")

    def create_mock_update(self, chat_id):
        update = MagicMock()
        update.effective_chat.id = chat_id
        update.message = AsyncMock()
        return update

    async def test_cmd_enable_unauthorized(self):
        # Set state to disabled first
        await main.state.disable()
        self.assertFalse(main.state.enabled)

        # Unauthorized chat ID
        update = self.create_mock_update(67890)
        context = MagicMock()

        await main.cmd_enable(update, context)

        # Verify it remained disabled (FIXED)
        self.assertFalse(main.state.enabled)
        update.message.reply_text.assert_not_called()

    async def test_cmd_disable_unauthorized(self):
        # Set state to enabled first
        await main.state.enable()
        self.assertTrue(main.state.enabled)

        # Unauthorized chat ID
        update = self.create_mock_update(67890)
        context = MagicMock()

        await main.cmd_disable(update, context)

        # Verify it remained enabled (FIXED)
        self.assertTrue(main.state.enabled)
        update.message.reply_text.assert_not_called()

    async def test_cmd_status_unauthorized(self):
        # Unauthorized chat ID
        update = self.create_mock_update(67890)
        context = MagicMock()

        await main.cmd_status(update, context)

        # Verify it did NOT respond (FIXED)
        update.message.reply_text.assert_not_called()

    async def test_cmd_enable_authorized(self):
        # Set state to disabled first
        await main.state.disable()
        self.assertFalse(main.state.enabled)

        # Authorized chat ID
        update = self.create_mock_update(12345)
        context = MagicMock()

        await main.cmd_enable(update, context)

        # Verify it WAS enabled
        self.assertTrue(main.state.enabled)
        update.message.reply_text.assert_called_with("✅ Notifications enabled.")

    async def test_cmd_disable_authorized(self):
        # Set state to enabled first
        await main.state.enable()
        self.assertTrue(main.state.enabled)

        # Authorized chat ID
        update = self.create_mock_update(12345)
        context = MagicMock()

        await main.cmd_disable(update, context)

        # Verify it WAS disabled
        self.assertFalse(main.state.enabled)
        update.message.reply_text.assert_called_with("🔕 Notifications disabled.")

if __name__ == "__main__":
    unittest.main()
