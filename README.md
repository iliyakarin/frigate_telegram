# 🔔 Frigate-Telegram

A Python bot that polls [Frigate NVR](https://frigate.video/) for detection events and sends rich notifications to Telegram — **one message per event** with an animated GIF preview, face recognition, and full event details.

> Inspired by [lucad87/frigate-telegram](https://github.com/lucad87/frigate-telegram), rebuilt from scratch in Python 3.11+ with modern async patterns and multi-camera support.

## ✨ Features

- **Single-message delivery** — GIF animation + event details in one Telegram message (no spam)
- **Face recognition** — displays recognized names from Frigate's `sub_label` field
- **Multi-camera matrix** — monitor specific cameras and zones via `MONITOR_CONFIG`
- **Cloudflare Tunnel support** — `EXTERNAL_URL` for secure public event links
- **Toggle notifications** — `/enable`, `/disable`, `/status`, and `/help` commands
- **Persistent state** — notification toggle survives container restarts (JSON file)
- **Retry logic** — automatically retries media fetches if Frigate hasn't generated them yet
- **Graceful fallback** — GIF → snapshot → thumbnail → text-only if media isn't available
- **Tunnel-safe timeouts** — configurable `UPLOAD_TIMEOUT` for slow connections
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
| `MONITOR_CONFIG` | ❌ | *(all)* | Camera/zone matrix — see [below](#-monitor-config) |
| `EXTERNAL_URL` | ❌ | — | Public Frigate URL for clickable event links (e.g. via Cloudflare Tunnel) |
| `FRIGATE_USERNAME` | ❌ | — | Basic auth username (if Frigate auth is enabled) |
| `FRIGATE_PASSWORD` | ❌ | — | Basic auth password |
| `POLLING_INTERVAL` | ❌ | `60` | Seconds between polls |
| `MEDIA_WAIT_TIMEOUT` | ❌ | `5` | Seconds to wait before fetching media (lets Frigate generate previews) |
| `UPLOAD_TIMEOUT` | ❌ | `60` | Seconds for Telegram upload timeout (increase for slow tunnels) |
| `TIMEZONE` | ❌ | `UTC` | Timezone for timestamps (e.g. `America/Chicago`) |
| `LOCALES` | ❌ | `en-US` | Locale for date formatting |
| `DEBUG` | ❌ | `false` | Enable verbose logging |

## 📷 Monitor Config

To monitor all cameras and zones, just comment out (or omit) the `MONITOR_CONFIG` variable in `docker-compose.yml`.

Or use `MONITOR_CONFIG` to define **which cameras and zones** to watch with a simple semicolon-separated syntax:

```bash
# JSON format (preferred)
MONITOR_CONFIG='{"camera1": ["zone_a", "zone_b"], "camera2": ["all"]}'

# Legacy semicolon format (still supported)
MONITOR_CONFIG=camera1:zone_a,zone_b;camera2:all
```


### Syntax

| Format | Meaning |
|---|---|
| `'{"cam": ["z1"]}'` | (JSON) Monitor `cam` in `z1` |
| `cam:z1,z2` | (Legacy) Monitor `cam` in `z1` and `z2` |
| `cam:all` | Monitor `cam` in **all zones** |
| *(empty)* | Monitor **all cameras/all zones** |


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

## 🌐 External URL & Cloudflare Tunnel

The `EXTERNAL_URL` variable enables **clickable event links** in Telegram notifications that point to your Frigate web UI.

This is designed for use with [Cloudflare Tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (`cloudflared`), which provide secure external access to your Frigate instance **without opening ports** on your router.

### How it works

```
Your Phone  →  Telegram  →  Click "View Event in Frigate"
                              ↓
                   https://cctv.yourdomain.com/events/abc123
                              ↓
                   Cloudflare Tunnel (cloudflared)
                              ↓
                   Frigate NVR (LAN, e.g. 192.168.1.100:5000)
```

### Setup

1. **Install `cloudflared`** on the machine running Frigate (or on the same network)
2. **Create a tunnel** pointing to your Frigate instance:
   ```bash
   cloudflared tunnel --url http://localhost:5000 --name frigate
   ```
3. **Map a DNS hostname** (e.g. `cctv.yourdomain.com`) to the tunnel in Cloudflare dashboard
4. **Set `EXTERNAL_URL`** in your `docker-compose.yml`:
   ```yaml
   - EXTERNAL_URL=https://cctv.yourdomain.com
   ```

The bot will generate event links like: `https://cctv.yourdomain.com/events/<event_id>`

> **Note:** If `EXTERNAL_URL` is not set, event links will not be included in notifications. A direct clip download link via the internal Frigate API is always included.

### Upload Timeouts

When running behind a Cloudflare Tunnel on a connection with limited upload bandwidth (e.g. AT&T fiber behind double NAT, Keenetic router), media uploads to Telegram can be slow. The `UPLOAD_TIMEOUT` variable (default: 60s) prevents the bot from hanging:

```yaml
- UPLOAD_TIMEOUT=120  # Increase for very slow upload speeds
```

## 👤 Face Recognition

If you have Frigate's face recognition configured, the bot will automatically display recognized names in notifications.

Frigate stores recognized faces in the `sub_label` field of event data. When present:

- **Recognized face:** `👤 Name: John (95%)`
- **Unknown/no face:** `🏷️ Name: Person (92%)`

No additional configuration is needed — the bot reads `sub_label` directly from the Frigate event API.

## 🤖 Telegram Commands

| Command | Description | Aliases |
|---|---|---|
| `/start` | Show welcome message | — |
| `/help` | Show available commands | — |
| `/enable_notifications` | Turn on event notifications | `/enable` |
| `/disable_notifications` | Turn off event notifications | `/disable` |
| `/status` | Show bot status and configuration | — |

## 📦 Docker Compose

```yaml
services:
  frigate-telegram:
    image: ghcr.io/ikarin/frigate-telegram:latest
    container_name: frigate-telegram
    restart: unless-stopped
    environment:
      - FRIGATE_URL=http://frigate:5000
      - EXTERNAL_URL=https://cctv.yourdomain.com
      - TELEGRAM_BOT_TOKEN=123456:ABC-DEF
      - TELEGRAM_CHAT_ID=-1001234567890
      - MONITOR_CONFIG={"front_door": ["yard", "driveway"], "back_camera": ["all"]}
      - POLLING_INTERVAL=60
      - MEDIA_WAIT_TIMEOUT=5
      - UPLOAD_TIMEOUT=60
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

� Name: John (95%)
📍 Location: front_door — yard
📅 Time: 2025-01-15 14:32:10 CST

🔗 View Event in Frigate
🎬 Download Event Clip
```

If no face is recognized:
```
🚨 Detection Alert

🏷️ Name: Person (92%)
📍 Location: front_door — yard, driveway
📅 Time: 2025-01-15 14:32:10 CST
🕑 End: 2025-01-15 14:32:45 CST

🔗 View Event in Frigate
🎬 Download Event Clip
```

The animated GIF preview is attached as the main media of the message.

## 📄 License

MIT
