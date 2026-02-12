# 🔔 Frigate-Telegram

A Python bot that polls [Frigate NVR](https://frigate.video/) for detection events and sends rich notifications to Telegram — **one message per event** with an animated GIF preview and full event details.

> Inspired by [lucad87/frigate-telegram](https://github.com/lucad87/frigate-telegram), rebuilt from scratch in Python 3.11+ with modern async patterns and multi-camera support.

## ✨ Features

- **Single-message delivery** — GIF animation + event details in one Telegram message (no spam)
- **Multi-camera matrix** — monitor specific cameras and zones via `MONITOR_CONFIG`
- **Toggle notifications** — `/enable_notifications`, `/disable_notifications`, `/status` commands
- **Persistent state** — notification toggle survives container restarts (JSON file)
- **Retry logic** — automatically retries media fetches if Frigate hasn't generated them yet
- **Graceful fallback** — GIF → thumbnail → text-only if media isn't available
- **Optimized Docker image** — slim Python base, ~60MB

## 🚀 Quick Start

### 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **bot token**
4. Send a message to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your **chat ID**

### 2. Configure & Run

```bash
# Clone and configure
git clone https://github.com/your-user/frigate-telegram.git
cd frigate-telegram
cp docker-compose.yml docker-compose.override.yml
# Edit docker-compose.override.yml with your settings

# Start the bot
docker compose up -d

# View logs
docker compose logs -f
```

## ⚙️ Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `FRIGATE_URL` | ✅ | — | Internal URL of your Frigate instance (e.g. `http://192.168.1.100:5000`) |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Target chat/group ID for notifications |
| `MONITOR_CONFIG` | ❌ | *(all)* | Camera/zone matrix — see [below](#monitor-config) |
| `FRIGATE_EXTERNAL_URL` | ❌ | `FRIGATE_URL` | Public URL used in clickable clip links |
| `FRIGATE_USERNAME` | ❌ | — | Basic auth username (if Frigate auth is enabled) |
| `FRIGATE_PASSWORD` | ❌ | — | Basic auth password |
| `POLLING_INTERVAL` | ❌ | `60` | Seconds between polls |
| `TIMEZONE` | ❌ | `UTC` | Timezone for timestamps (e.g. `America/Chicago`) |
| `LOCALES` | ❌ | `en-US` | Locale for date formatting |
| `DEBUG` | ❌ | `false` | Enable verbose logging |

## 📷 Monitor Config

The `MONITOR_CONFIG` variable defines **which cameras and zones** to watch. It uses a simple semicolon-separated syntax:

```
MONITOR_CONFIG=camera1:zone_a,zone_b;camera2:all;camera3:driveway
```

### Syntax

| Format | Meaning |
|---|---|
| `camera1:zone_a,zone_b` | Monitor `camera1`, only in `zone_a` and `zone_b` |
| `camera2:all` | Monitor `camera2` in **all zones** |
| `camera3` | Same as `camera3:all` |
| *(empty / omitted)* | Monitor **all cameras** and **all zones** |

### Examples

```bash
# Single camera, specific zones
MONITOR_CONFIG=front_door:yard,driveway

# Multiple cameras with different zone filters
MONITOR_CONFIG=front_door:yard,porch;back_camera:all;garage:driveway

# All zones on all listed cameras
MONITOR_CONFIG=front_door;back_camera;garage

# Monitor everything (omit the variable entirely)
# MONITOR_CONFIG=
```

## 🤖 Telegram Commands

| Command | Description |
|---|---|
| `/enable_notifications` | Turn on event notifications |
| `/disable_notifications` | Turn off event notifications |
| `/status` | Show current bot status, polling interval, and monitored cameras |

## 📦 Docker Compose

```yaml
services:
  frigate-telegram:
    build: .
    container_name: frigate-telegram
    restart: unless-stopped
    environment:
      - FRIGATE_URL=http://192.168.1.100:5000
      - FRIGATE_EXTERNAL_URL=https://frigate.example.com
      - TELEGRAM_BOT_TOKEN=123456:ABC-DEF
      - TELEGRAM_CHAT_ID=-1001234567890
      - MONITOR_CONFIG=front_door:yard,driveway;back_camera:all
      - POLLING_INTERVAL=60
      - TIMEZONE=America/Chicago
      - DEBUG=false
    volumes:
      - frigate-telegram-data:/app/data

volumes:
  frigate-telegram-data:
```

## 🔧 Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables (or create a .env file)
export FRIGATE_URL=http://localhost:5000
export TELEGRAM_BOT_TOKEN=your-token
export TELEGRAM_CHAT_ID=your-chat-id

# Run
python main.py
```

## 📝 Notification Example

Each notification is a **single Telegram message** containing:

```
🚨 Detection Alert

📷 Camera: front_door
🏷️ Label: person (92%)
📍 Zone(s): yard, driveway
🕐 Start: 2025-01-15 14:32:10 CST
🕑 End: 2025-01-15 14:32:45 CST

🎬 View Event Clip
```

The animated GIF preview is attached as the main media of the message.

## 📄 License

MIT
