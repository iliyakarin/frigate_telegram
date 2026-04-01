import time
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import sys
sys.modules['httpx'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.constants'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()

import main

async def benchmark_fetch_camera_list():
    client = MagicMock()
    # Mock the get call to simulate network delay
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"cameras": {"front": {}, "back": {}}}

    async def mock_get(*args, **kwargs):
        await asyncio.sleep(0.5) # 500ms network delay
        return mock_resp

    client.get = mock_get

    start = time.time()
    for _ in range(10):
        await main.fetch_camera_list(client)
    end = time.time()
    print(f"Total time for 10 calls: {end - start:.2f} seconds")

if __name__ == "__main__":
    asyncio.run(benchmark_fetch_camera_list())
