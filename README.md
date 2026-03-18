# Frigate Notify (Telegram)

A small notifier service that listens to Frigate MQTT review events and sends a photo alert to a Telegram chat when a person is detected in a configured zone sequence.

## ✅ Features

- Subscribes to Frigate `frigate/events` MQTT topic
- Filters events for `person` and a configured zone sequence
- Downloads the latest camera image from Frigate over HTTP
- Sends the image as a Telegram photo message to a configured chat

## 🧩 Requirements

- Python 3.9+
- MQTT broker reachable by the service
- Frigate HTTP API reachable by the service
- Telegram bot token + target chat ID

## 🚀 Quick-start (Docker Compose)

1. Copy and update the environment values in `docker-compose.yml` and set Frigate's base URL so latest images can be fetched:

```yaml
services:
  frigate-telegram:
    build: .
    restart: unless-stopped

    environment:
      MQTT_BROKER: mqtt
      MQTT_USER: mqtt_user
      MQTT_PASSWORD: mqtt_password
      FRIGATE_URL: http://frigate:5000

      BOT_TOKEN: YOUR_TELEGRAM_TOKEN
      CHAT_ID: -1001234567890

      ZONE_SEQUENCE: Street,Pavers,Door,Porch
```

2. Start the service:

```bash
docker compose up -d
```

3. Verify logs for startup and incoming events:

```bash
docker compose logs -f
```

## 📦 Published image (GitHub Container Registry)

You can pull the latest image with:

```bash
docker pull ghcr.io/ivanjx/frigate-notify:latest
```

## ▶️ Running locally (Python)

1. Create a virtual environment and install dependencies:

```bash
python -m venv .env
. .env/Scripts/activate   # Windows PowerShell
pip install -r requirements.txt
```

2. Set required environment variables:

- `BOT_TOKEN` - your Telegram bot token (from @BotFather)
- `CHAT_ID` - target chat or channel ID (e.g. `-1001234567890`)

Optional variables:
- `MQTT_BROKER` (default: `mqtt`)
- `MQTT_USER` (optional)
- `MQTT_PASSWORD` (optional)
- `FRIGATE_URL` (required, e.g. `http://frigate:5000`)
- `ZONE_SEQUENCE` (default: `Pavers,Door`)

3. Run the service:

```bash
python app.py
```

## 🔧 Configuration

### Frigate image source

The service fetches the latest image for the matched camera from `FRIGATE_URL/api/{cam}/latest.jpg` and sends that image to Telegram.

### Zone sequence filtering

`ZONE_SEQUENCE` controls the required zone order in the Frigate event payload. The service will send a Telegram alert only when the event zones include the provided list in order.

Example:

```env
ZONE_SEQUENCE=Street,Pavers,Door,Porch
```

## 🧪 Troubleshooting

- If the app exits immediately, verify `BOT_TOKEN` and `CHAT_ID` are set.
- If Telegram send fails, check logs for the Telegram API response (it will print the returned JSON error).
- If images are missing, verify `FRIGATE_URL` is set and reachable from the container.
