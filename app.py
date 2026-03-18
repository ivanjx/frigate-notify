import json
import os
import time
import random
import requests
import threading
import concurrent.futures
import paho.mqtt.client as mqtt

MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "frigate/reviews")
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
ZONE_SEQUENCE = [
    z.strip() for z in os.getenv("ZONE_SEQUENCE", "Pavers,Door").split(",")
]

RETRY_MAX_ATTEMPTS = int(os.getenv("TELEGRAM_RETRY_ATTEMPTS", "5"))
RETRY_BACKOFF_BASE = float(os.getenv("TELEGRAM_RETRY_BACKOFF_BASE", "1.0"))
RETRY_BACKOFF_MAX = float(os.getenv("TELEGRAM_RETRY_BACKOFF_MAX", "16.0"))
REQUEST_TIMEOUT = float(os.getenv("TELEGRAM_REQUEST_TIMEOUT", "20"))

if not MQTT_BROKER:
    raise SystemExit("Missing required environment variable: MQTT_BROKER")
if not BOT_TOKEN:
    raise SystemExit("Missing required environment variable: BOT_TOKEN")
if not CHAT_ID:
    raise SystemExit("Missing required environment variable: CHAT_ID")
if not ZONE_SEQUENCE:
    raise SystemExit("Missing required environment variable: ZONE_SEQUENCE")

NOTIFIED_AT: dict[str, float] = {}
NOTIFIED_AT_LOCK = threading.Lock()
NOTIFY_SUPPRESSION_SECONDS = 10 * 60  # 10 minutes

MQTT_WORKER_THREADS = max(1, os.cpu_count() or 1)
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MQTT_WORKER_THREADS)


def post_with_retries(url, data=None, files=None, timeout=REQUEST_TIMEOUT):
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as e:
            # network-level error, retry
            if attempt == RETRY_MAX_ATTEMPTS - 1:
                print(f"Telegram request failed after {attempt+1} attempts: {e}")
                return None
            backoff = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (2 ** attempt)) + random.random()
            time.sleep(backoff)
            continue

        # Handle rate limiting explicitly (Telegram may include parameters.retry_after)
        if resp.status_code == 429:
            try:
                body = resp.json()
                retry_after = body.get("parameters", {}).get("retry_after")
                sleep_for = float(retry_after) + 0.5 if retry_after is not None else None
            except ValueError:
                sleep_for = None

            if sleep_for is None:
                sleep_for = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (2 ** attempt)) + random.random()

            if attempt == RETRY_MAX_ATTEMPTS - 1:
                print("Telegram rate limited and max retries reached")
                return resp

            time.sleep(sleep_for)
            continue

        # Retry on server errors
        if 500 <= resp.status_code < 600:
            if attempt == RETRY_MAX_ATTEMPTS - 1:
                return resp
            backoff = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (2 ** attempt)) + random.random()
            time.sleep(backoff)
            continue

        # For 4xx (other than 429) do not retry
        return resp

    return None


def send_telegram(text, file_paths):
    # Normalize to a list of paths
    if file_paths is None:
        file_paths = []
    elif isinstance(file_paths, str):
        file_paths = [file_paths]

    img_entries = []
    for fp in file_paths:
        try:
            with open(fp, "rb") as f:
                img_bytes = f.read()
            img_entries.append((fp, img_bytes))
        except Exception as e:
            print(f"Failed to read image file '{fp}': {e}")
            continue

    # No images -> send text message
    if not img_entries:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": text}
        resp = post_with_retries(url, data=data)
        if resp is None:
            return False

        try:
            resp.raise_for_status()
        except requests.RequestException as e:
            details = resp.text if 'resp' in locals() else ''
            print(f"Telegram send failed: {e} — {details}")
            return False

        try:
            json_resp = resp.json()
        except ValueError:
            details = resp.text if 'resp' in locals() else ''
            print(f"Telegram send failed: invalid JSON response — {details}")
            return False

        if not json_resp.get("ok"):
            print("Telegram API returned error:", json_resp)
            return False
        return True

    # Single image -> sendPhoto
    if len(img_entries) == 1:
        fp, img_bytes = img_entries[0]
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        filename = os.path.basename(fp) if fp else "photo.jpg"
        files = {"photo": (filename, img_bytes, "image/jpeg")}
        data = {"chat_id": CHAT_ID, "caption": text}
        resp = post_with_retries(url, data=data, files=files)
        if resp is None:
            return False

        try:
            resp.raise_for_status()
        except requests.RequestException as e:
            details = resp.text if 'resp' in locals() else ''
            print(f"Telegram send failed: {e} — {details}")
            return False

        try:
            json_resp = resp.json()
        except ValueError:
            details = resp.text if 'resp' in locals() else ''
            print(f"Telegram send failed: invalid JSON response — {details}")
            return False

        if not json_resp.get("ok"):
            print("Telegram API returned error:", json_resp)
            return False
        return True

    # Multiple images -> send all images in a single sendMediaGroup
    success = True
    files = {}
    media = []
    for idx, (fp, img_bytes) in enumerate(img_entries):
        field_name = f"photo{idx}"
        filename = os.path.basename(fp) if fp else f"photo{idx}.jpg"
        files[field_name] = (filename, img_bytes, "image/jpeg")
        item = {"type": "photo", "media": f"attach://{field_name}"}
        # include caption on the first media item
        if idx == 0:
            item["caption"] = text
        media.append(item)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMediaGroup"
    data = {"chat_id": CHAT_ID, "media": json.dumps(media)}
    resp = post_with_retries(url, data=data, files=files)
    if resp is None:
        return False

    try:
        resp.raise_for_status()
        json_resp = resp.json()
        if not json_resp.get("ok"):
            print("Telegram API returned error:", json_resp)
            success = False
    except requests.RequestException as e:
        details = resp.text if 'resp' in locals() else ''
        print(f"Telegram send failed: {e} — {details}")
        success = False
    except ValueError:
        details = resp.text if 'resp' in locals() else ''
        print(f"Telegram send failed: invalid JSON response — {details}")
        success = False

    return success


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
    # Submit processing to the worker pool so the MQTT network thread is not blocked
    try:
        executor.submit(handle_message, msg)
    except Exception as e:
        print(f"Failed to submit MQTT message to worker pool: {e}")


# Worker function runs in a background thread
def handle_message(msg: mqtt.MQTTMessage):
    try:
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
        with NOTIFIED_AT_LOCK:
            last_sent = NOTIFIED_AT.get(review_id)
            if last_sent is not None and (now - last_sent) < NOTIFY_SUPPRESSION_SECONDS:
                print(f"Notification already sent for review id {review_id}")
                return

        # Send notification
        data = after.get("data", {})
        detections = data.get("detections", [])
        camera = after.get("camera")
        file_paths = []
        if detections and camera:
            for det in detections:
                file_paths.append(os.path.join("/media/frigate/clips", f"{camera}-{det}.jpg"))

        send_status = send_telegram(f"Entrance detected\nCamera: {camera}", file_paths[:10])
        if send_status:
            with NOTIFIED_AT_LOCK:
                NOTIFIED_AT[review_id] = now

        # Periodically prune old entries to avoid unbounded memory growth
        with NOTIFIED_AT_LOCK:
            if len(NOTIFIED_AT) > 1000:
                cutoff = now - NOTIFY_SUPPRESSION_SECONDS * 2
                for k, v in list(NOTIFIED_AT.items()):
                    if v < cutoff:
                        NOTIFIED_AT.pop(k, None)
    except Exception as e:
        print(f"Exception in MQTT message handler: {e}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

if MQTT_USER:
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

client.connect(MQTT_BROKER)
client.subscribe(MQTT_TOPIC)
client.loop_forever()
