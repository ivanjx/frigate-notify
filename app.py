import json
import os
import time
import requests
import paho.mqtt.client as mqtt

MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "frigate/reviews")
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
FRIGATE_URL = os.getenv("FRIGATE_URL", "http://frigate:5000")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ZONE_SEQUENCE = [
    z.strip() for z in os.getenv("ZONE_SEQUENCE", "Pavers,Door").split(",")
]

if not MQTT_BROKER:
    raise SystemExit("Missing required environment variable: MQTT_BROKER")
if not FRIGATE_URL:
    raise SystemExit("Missing required environment variable: FRIGATE_URL")
if not BOT_TOKEN:
    raise SystemExit("Missing required environment variable: BOT_TOKEN")
if not CHAT_ID:
    raise SystemExit("Missing required environment variable: CHAT_ID")
if not ZONE_SEQUENCE:
    raise SystemExit("Missing required environment variable: ZONE_SEQUENCE")

# Keep track of when we last notified for each review id so we don't spam multiple updates
# (Frigate can emit 'new', 'update', 'end' events for the same id).
NOTIFIED_AT: dict[str, float] = {}
NOTIFY_SUPPRESSION_SECONDS = 10 * 60  # 10 minutes


def send_telegram(text, image_url):
    img_bytes = None
    try:
        r = requests.get(image_url, timeout=10)
        r.raise_for_status()
        img_bytes = r.content
    except Exception as e:
        print(f"Failed to download image: {e}")

    if img_bytes:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        files = {
            "photo": ("photo.jpg", img_bytes, "image/jpeg")
        }
        data = {
            "chat_id": CHAT_ID,
            "caption": text
        }
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        files = None
        data = {
            "chat_id": CHAT_ID,
            "text": text
        }

    try:
        resp = requests.post(url, data=data, files=files, timeout=20)
        resp.raise_for_status()
        json_resp = resp.json()
        if not json_resp.get("ok"):
            print("Telegram API returned error:", json_resp)
            return False
        return True
    except requests.RequestException as e:
        details = resp.text if 'resp' in locals() else ''
        print(f"Telegram send failed: {e} — {details}")
        return False
    except ValueError:
        details = resp.text if 'resp' in locals() else ''
        print(f"Telegram send failed: invalid JSON response — {details}")
        return False


def on_connect(client, userdata, flags, rc, properties=None):
    status = "success" if rc == 0 else f"error code {rc}"
    print(f"Connected to MQTT broker '{MQTT_BROKER}' ({status})")


def zones_in_order(zones, required):
    pos = 0

    for z in zones:
        if pos < len(required) and z == required[pos]:
            pos += 1
            if pos == len(required):
                return True

    return False


def on_message(client, userdata, msg: mqtt.MQTTMessage):
    # Parse the payload
    print(f"MQTT message received: payload={msg.payload.decode('utf-8', errors='replace')}")
    payload = json.loads(msg.payload)
    after = payload["after"]
    data = after["data"]
    objects = data.get("objects", [])
    zones = data.get("zones", [])
    review_id = after["id"]

    # Compare
    if "person" not in objects:
        return

    if not zones_in_order(zones, ZONE_SEQUENCE):
        return

    # Do not resend notifications for the same review id within the suppression window
    now = time.time()
    last_sent = NOTIFIED_AT.get(review_id)
    if last_sent is not None and (now - last_sent) < NOTIFY_SUPPRESSION_SECONDS:
        print(f"Notification already sent for review id {review_id}")
        return

    # Send notification
    snapshot = f"{FRIGATE_URL}/api/review/thumb/{review_id}.webp"
    send_status = send_telegram(
        f"Entrance detected\nCamera: {after['camera']}",
        snapshot
    )
    if send_status:
        NOTIFIED_AT[review_id] = now

    # Periodically prune old entries to avoid unbounded memory growth
    if len(NOTIFIED_AT) > 1000:
        cutoff = now - NOTIFY_SUPPRESSION_SECONDS * 2
        for k, v in list(NOTIFIED_AT.items()):
            if v < cutoff:
                NOTIFIED_AT.pop(k, None)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

if MQTT_USER:
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

client.connect(MQTT_BROKER)
client.subscribe(MQTT_TOPIC)

client.loop_forever()
