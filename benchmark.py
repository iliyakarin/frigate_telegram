import sys
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

# Mock third-party dependencies before importing main
sys.modules['httpx'] = MagicMock()
sys.modules['dotenv'] = MagicMock()
sys.modules['telegram'] = MagicMock()
sys.modules['telegram.constants'] = MagicMock()
sys.modules['telegram.ext'] = MagicMock()

import main

async def run_benchmark():
    # Mock bot
    bot = MagicMock()
    bot.send_video = AsyncMock()
    bot.send_animation = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()

    # Mock HTTP client
    http_client = MagicMock()

    # Disable wait timeout
    main.MEDIA_WAIT_TIMEOUT = 0
    main.SEND_CLIP = True

    event = {"id": "123", "camera": "front_door"}

    total_bytes_downloaded = 0
    total_requests_made = 0

    # Mock fetch_event_media
    async def mock_fetch_event_media(client, event_id, media_type):
        nonlocal total_bytes_downloaded, total_requests_made
        total_requests_made += 1
        await asyncio.sleep(0.1) # simulate network delay
        size = 0
        if media_type == "clip":
            size = 5_000_000 # 5MB
        elif media_type == "gif":
            size = 2_000_000 # 2MB
        elif media_type == "thumbnail":
            size = 50_000 # 50KB
        total_bytes_downloaded += size
        return b"x" * size

    # Mock fetch_camera_snapshot
    async def mock_fetch_camera_snapshot(client, camera):
        nonlocal total_bytes_downloaded, total_requests_made
        total_requests_made += 1
        await asyncio.sleep(0.1) # simulate network delay
        size = 300_000 # 300KB
        total_bytes_downloaded += size
        return b"x" * size

    async def mock_fetch_event_details(client, event_id):
        return event

    with patch('main.fetch_event_media', side_effect=mock_fetch_event_media):
        with patch('main.fetch_camera_snapshot', side_effect=mock_fetch_camera_snapshot):
            with patch('main.fetch_event_details', side_effect=mock_fetch_event_details):
                start_time = time.time()
                await main.send_event_notification(bot, event, http_client)
                end_time = time.time()

    print(f"Benchmark Results:")
    print(f"Time Taken: {end_time - start_time:.4f} seconds")
    print(f"Total Bytes Downloaded: {total_bytes_downloaded / 1_000_000:.2f} MB")
    print(f"Total Requests Made: {total_requests_made}")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
