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
from grouping import PendingGroup

async def run_benchmark():
    # Mock bot
    bot = MagicMock()
    bot.send_video = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()

    # Mock HTTP client
    http_client = MagicMock()

    group = PendingGroup(
        camera="front_door",
        labels={"person"},
        review_ids=["rev_123"],
        event_ids={"evt_123"},
        first_start=1000,
        last_activity_end=1030,
        last_seen_at=1030,
    )

    total_bytes_downloaded = 0
    total_requests_made = 0

    # Mock fetch_recording_clip — the always-used clip source for grouped notifications
    async def mock_fetch_recording_clip(client, camera, start_ts, end_ts):
        nonlocal total_bytes_downloaded, total_requests_made
        total_requests_made += 1
        await asyncio.sleep(0.1) # simulate network delay
        size = 5_000_000 # 5MB
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
        return {
            "id": event_id, "camera": "front_door", "label": "person",
            "zones": [], "start_time": 1000, "end_time": 1030,
        }

    with patch('main.fetch_recording_clip', side_effect=mock_fetch_recording_clip):
        with patch('main.fetch_camera_snapshot', side_effect=mock_fetch_camera_snapshot):
            with patch('main.fetch_event_details', side_effect=mock_fetch_event_details):
                start_time = time.time()
                await main.send_grouped_notification(bot, group, http_client)
                end_time = time.time()

    print(f"Benchmark Results:")
    print(f"Time Taken: {end_time - start_time:.4f} seconds")
    print(f"Total Bytes Downloaded: {total_bytes_downloaded / 1_000_000:.2f} MB")
    print(f"Total Requests Made: {total_requests_made}")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
