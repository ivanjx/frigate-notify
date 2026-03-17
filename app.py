import json
import os
import requests
import paho.mqtt.client as mqtt

MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "frigate/reviews")
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

FRIGATE_URL = os.getenv("FRIGATE_URL", "http://frigate:5000")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN:
    raise SystemExit("Missing required environment variable: BOT_TOKEN")
if not CHAT_ID:
    raise SystemExit("Missing required environment variable: CHAT_ID")

ZONE_SEQUENCE = [
    z.strip() for z in os.getenv("ZONE_SEQUENCE", "Pavers,Door").split(",")
]


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


def zones_in_order(zones, required):
    pos = 0

    for z in zones:
        if pos < len(required) and z == required[pos]:
            pos += 1
            if pos == len(required):
                return True

    return False


def on_connect(client, userdata, flags, rc, properties=None):
    status = "success" if rc == 0 else f"error code {rc}"
    print(f"Connected to MQTT broker '{MQTT_BROKER}' ({status})")


def on_message(client, userdata, msg: mqtt.MQTTMessage):
    print(f"MQTT message received: payload={msg.payload.decode('utf-8', errors='replace')}")
    payload = json.loads(msg.payload)

    after = payload["after"]
    data = after["data"]
    objects = data.get("objects", [])
    zones = data.get("zones", [])

    if "person" not in objects:
        return

    if not zones_in_order(zones, ZONE_SEQUENCE):
        return

    review_id = after["id"]
    snapshot = f"{FRIGATE_URL}/api/review/thumb/{review_id}.webp"
    send_telegram(
        f"Entrance detected\nCamera: {after['camera']}",
        snapshot
    )


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

if MQTT_USER:
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

client.connect(MQTT_BROKER)
client.subscribe(MQTT_TOPIC)

client.loop_forever()
