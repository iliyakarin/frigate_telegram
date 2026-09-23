# 🔔 Frigate-Telegram

A Python bot that polls [Frigate NVR](https://frigate.video/) for detection events and sends rich notifications to Telegram — **one message per activity**, spanning brief pauses so one visit doesn't fragment into several short clips, with a full HD video, face recognition, and full event details.

> Inspired by [lucad87/frigate-telegram](https://github.com/lucad87/frigate-telegram), rebuilt from scratch in Python 3.11+ with modern async patterns and multi-camera support.

## ✨ Features

- **Single-message delivery** — full HD video clip(s) + event details under one shared caption per activity (no spam)
- **Camera health alerts** — automated detection of disconnected cameras or 0 FPS streams with 60s debounce, repeat escalation schedule (+5m, +60m, +12h, +24h), recovery notifications with downtime tracking, and error log extraction
- **Event grouping** — merges rapid-fire Frigate events on the same camera into one notification spanning the whole activity, sent in chronological order under one shared caption — one video per constituent event (tagged "Event N/M" when more than one), instead of several short out-of-order clips (`EVENT_MERGE_GAP`, `MAX_EVENT_SPAN`)
- **Face recognition** — displays recognized names from Frigate's `sub_label` field
- **Multi-camera matrix** — monitor specific cameras and zones via `MONITOR_CONFIG`
- **Cloudflare Tunnel support** — `EXTERNAL_URL` for secure public event links
- **Toggle notifications** — `/enable`, `/disable`, `/status`, and `/help` commands
- **Persistent state** — notification toggle survives container restarts (JSON file)
- **Retry logic** — automatically retries media fetches if Frigate hasn't generated them yet
- **Graceful fallback** — HD video → GIF preview → snapshot → text-only if media isn't available
- **Long clips** — a clip over Telegram's 50 MB limit is split into N parts tagged "Part i/N" (at most 10 are sent; the last one notes the truncation)
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
| `HEALTH_MONITOR_CAMERAS` | ❌ | *(all)* | Comma-separated or JSON list of cameras to monitor for health alerts (defaults to all) |
| `HEALTH_CACHE_THRESHOLD_PCT` | ❌ | `85` | Alert when Frigate's `/tmp/cache` usage reaches this percent (1–100); a full cache means recordings stopped being saved |
| `EXTERNAL_URL` | ❌ | — | Public Frigate URL for clickable event links (e.g. via Cloudflare Tunnel) |
| `FRIGATE_USERNAME` | ❌ | — | Basic auth username (if Frigate auth is enabled) |
| `FRIGATE_PASSWORD` | ❌ | — | Basic auth password |
| `POLLING_INTERVAL` | ❌ | `60` | Seconds between polls |
| `EVENT_MERGE_GAP` | ❌ | `45` | Seconds of quiet before finalizing a notification — related activity on the same camera within this gap gets merged into one message |
| `MAX_EVENT_SPAN` | ❌ | `300` | Hard cap (seconds) on a merged notification's duration, so continuously recurring activity still gets sent eventually |
| `CLIP_PADDING_SECONDS` | ❌ | `5` | Extra seconds included before each event starts and after it ends in that event's sent clip |
| `UPLOAD_TIMEOUT` | ❌ | `60` | Seconds for Telegram upload timeout (increase for slow tunnels) |
| `MAX_TELEGRAM_FILE_SIZE` | ❌ | `52428800` | Max bytes per uploaded video (Telegram bot limit, 50 MB). Larger clips are split into up to 10 time-sliced parts |
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

## 🩺 Camera Health Alerts

The bot continuously monitors camera connection quality and frame rates via Frigate's `/api/stats` endpoint.

- **Debounced Detection:** Cameras must be continuously disconnected or at 0 FPS for at least **60 seconds** before an alert triggers, preventing false alarms from brief network hiccups.
- **Escalation Schedule:** Alerts are repeated up to 5 times on an escalating schedule:
  - Initial Alert: After 60s continuous failure
  - Alert 2: +5 minutes
  - Alert 3: +60 minutes
  - Alert 4: +12 hours
  - Alert 5: +24 hours (silenced after Alert 5 until recovery)
- **Dual-Check Error Details:** When an alert is triggered, the bot inspects Frigate's `/api/logs/frigate` to extract relevant ffmpeg/demuxing errors. If log access is unavailable, it falls back cleanly to stats data.
- **Recovery Notification:** When a camera reconnects and begins receiving frames again, the bot sends a recovery alert with the total downtime duration and resets all counters.
- **Configurable Scope:** By default, all cameras reported by Frigate are monitored. Use `HEALTH_MONITOR_CAMERAS` to limit monitoring to specific cameras.
- **Recording cache alert:** The bot also watches Frigate's `/tmp/cache` usage (same `/api/stats` payload). If it stays at or above `HEALTH_CACHE_THRESHOLD_PCT` (default 85%), Frigate's recording maintainer has likely stalled and clips come back empty — you get an alert (same debounce and escalation schedule as cameras) suggesting a Frigate restart, then a recovery message once the cache drains. `/status` shows the current cache usage.
- **Independent of `/disable`:** Camera health alerts keep running even while event notifications are turned off via `/disable` — the two are checked independently every polling cycle.

## 🤖 Telegram Commands

| Command | Description |
|---|---|
| `/enable_notifications` | Turn on event notifications |
| `/disable_notifications` | Turn off event notifications |
| `/status` | Show bot status, polling interval, and real-time camera health |
| `/cameras` | List registered cameras with real-time health indicators (🟢 / 🔴) |
| `/menu` | Open the main interaction menu dashboard |
| `/photo [camera]` | Get a snapshot |
| `/photo_all` | Get current snapshots from all cameras |
| `/video [camera]` | Get 30s manual recording (requires server-side continuous recording enabled) |
| `/video_all` | Get 30s manual recording from all cameras (requires server-side continuous recording enabled) |
| `/video_last [camera]` | Get last event clip |
| `/video_all_last` | Get last event clips for all cameras |

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
      - EVENT_MERGE_GAP=45
      - MAX_EVENT_SPAN=300
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

If no video clip is available, the message falls back to an animated GIF preview of the event, then a snapshot photo, then text-only.

## 📄 License

MIT
