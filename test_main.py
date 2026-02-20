import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Mock dependencies that are not installed in the environment
mock_httpx = MagicMock()
sys.modules["httpx"] = mock_httpx

mock_telegram = MagicMock()
sys.modules["telegram"] = mock_telegram
sys.modules["telegram.constants"] = MagicMock()
sys.modules["telegram.ext"] = MagicMock()

# Now we can import main without ModuleNotFoundError
import main
import unittest

class TestCheckFrigateStatus(unittest.IsolatedAsyncioTestCase):
    @patch('main.FRIGATE_URL', 'http://frigate-test')
    @patch('main._http_auth')
    async def test_check_frigate_status_success(self, mock_auth):
        """Test successful status check."""
        mock_auth.return_value = None
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "1.0.0"
        mock_client.get.return_value = mock_response

        result = await main.check_frigate_status(mock_client)

        self.assertTrue(result)
        mock_client.get.assert_called_once_with(
            "http://frigate-test/api/version",
            auth=None,
            timeout=10
        )
        mock_response.raise_for_status.assert_called_once()

    @patch('main.FRIGATE_URL', 'http://frigate-test')
    @patch('main._http_auth')
    async def test_check_frigate_status_connection_error(self, mock_auth):
        """Test status check when a connection error occurs."""
        mock_auth.return_value = None
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection refused")

        result = await main.check_frigate_status(mock_client)

        self.assertFalse(result)
        mock_client.get.assert_called_once()

    @patch('main.FRIGATE_URL', 'http://frigate-test')
    @patch('main._http_auth')
    async def test_check_frigate_status_http_error(self, mock_auth):
        """Test status check when an HTTP error is returned."""
        mock_auth.return_value = None
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("HTTP Error")
        mock_client.get.return_value = mock_response

        result = await main.check_frigate_status(mock_client)

        self.assertFalse(result)
        mock_client.get.assert_called_once()
        mock_response.raise_for_status.assert_called_once()

if __name__ == '__main__':
    unittest.main()
