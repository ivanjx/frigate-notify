import json
import os
import time
import random
import requests
import threading
import concurrent.futures
import paho.mqtt.client as mqtt

MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt")
MQTT_TOPIC = "frigate/events"
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

NOTIFIED_AT: dict[str, float] = {}
NOTIFIED_AT_LOCK = threading.Lock()
NOTIFY_SUPPRESSION_SECONDS = 5 * 60  # 5 minutes

MQTT_WORKER_THREADS = max(1, os.cpu_count() or 1)
executor = concurrent.futures.ThreadPoolExecutor(max_workers=MQTT_WORKER_THREADS)


def post_with_retries(url, data=None, files=None, timeout=REQUEST_TIMEOUT):
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
        except requests.RequestException as e:
            # network-level error, retry
            print(f"Telegram request error on attempt {attempt+1}: {e}")
            if attempt == RETRY_MAX_ATTEMPTS - 1:
                print(f"Telegram request failed after {attempt+1} attempts: {e}")
                return None
            backoff = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (2 ** attempt)) + random.random()
            time.sleep(backoff)
            continue

        # Handle rate limiting explicitly (Telegram may include parameters.retry_after)
        if resp.status_code == 429:
            print("Telegram rate limited, retrying...")
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
            print(f"Telegram server error ({resp.status_code}), retrying...")
            if attempt == RETRY_MAX_ATTEMPTS - 1:
                return resp
            backoff = min(RETRY_BACKOFF_MAX, RETRY_BACKOFF_BASE * (2 ** attempt)) + random.random()
            time.sleep(backoff)
            continue

        # For other status codes (2xx, 4xx except 429), do not retry
        return resp

    return None


def send_telegram(text, file_path=None):
    img_bytes = None

    if file_path:
        try:
            with open(file_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            print(f"Failed to read image file '{file_path}': {e}")

    if not img_bytes:
        # Send without photo
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

    # Send with photo
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    files = {"photo": ("photo.jpg", img_bytes, "image/jpeg")}
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
    except ValueError:
        details = resp.text if 'resp' in locals() else ''
        print(f"Telegram send failed: invalid JSON response — {details}")
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


def zones_in_order(zones, required):
    pos = 0

    for z in zones:
        if pos < len(required) and z == required[pos]:
            pos += 1
            if pos == len(required):
                return True

    return False


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_event_context(payload):
    after = payload.get("after")
    if not isinstance(after, dict):
        return None

    data = after.get("data")
    if isinstance(data, dict):
        objects = _as_list(data.get("objects"))
        zones = _as_list(data.get("zones"))
    else:
        objects = _as_list(after.get("label"))
        zones = _as_list(after.get("entered_zones"))

    return {
        "event_type": payload.get("type"),
        "review_id": after.get("id"),
        "camera": after.get("camera"),
        "objects": objects,
        "zones": zones,
        "after": after,
    }


def handle_message(msg: mqtt.MQTTMessage):
    try:
        # Parse the payload
        print(f"MQTT message received: payload={msg.payload.decode('utf-8', errors='replace')}")
        payload = json.loads(msg.payload)
        context = extract_event_context(payload)
        if context is None:
            print("MQTT payload missing after data")
            return

        objects = context["objects"]
        zones = context["zones"]
        review_id = context["review_id"]
        camera = context["camera"]

        # Compare
        if "person" not in objects:
            return
        if not zones_in_order(zones, ZONE_SEQUENCE):
            return

        # Do not resend notifications for the same review id within the suppression window
        now = time.time()
        if review_id:
            with NOTIFIED_AT_LOCK:
                last_sent = NOTIFIED_AT.get(review_id)
                if last_sent is not None and (now - last_sent) < NOTIFY_SUPPRESSION_SECONDS:
                    print(f"Notification already sent for review id {review_id}")
                    return
                NOTIFIED_AT[review_id] = now

        # Send notification
        print(f"Sending Telegram notification for review id {review_id} with objects {objects} in zones {zones}")
        file_path = None
        if review_id and camera:
            file_path = os.path.join("/media/frigate/clips", f"{camera}-{review_id}.jpg")
        if file_path and not os.path.isfile(file_path):
            time.sleep(2) # Slight delay to allow file to be written by Frigate

        message_lines = ["Entrance detected"]
        if camera:
            message_lines.append(f"Camera: {camera}")
        send_status = send_telegram("\n".join(message_lines), file_path)
        if send_status:
            print(f"Telegram notification sent for review id {review_id}")
        else:
            print(f"Failed to send Telegram notification for review id {review_id}")

        # Periodically prune old entries to avoid unbounded memory growth
        with NOTIFIED_AT_LOCK:
            if len(NOTIFIED_AT) > 1000:
                cutoff = now - NOTIFY_SUPPRESSION_SECONDS * 2
                for k, v in list(NOTIFIED_AT.items()):
                    if v < cutoff:
                        NOTIFIED_AT.pop(k, None)
    except Exception as e:
        print(f"Exception in MQTT message handler: {e}")


def on_connect(client, userdata, flags, rc, properties=None):
    status = "success" if rc == 0 else f"error code {rc}"
    print(f"Connected to MQTT broker '{MQTT_BROKER}' ({status})")


def on_message(client, userdata, msg: mqtt.MQTTMessage):
    # Submit processing to the worker pool so the MQTT network thread is not blocked
    try:
        executor.submit(handle_message, msg)
    except Exception as e:
        print(f"Failed to submit MQTT message to worker pool: {e}")


def create_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    client.connect(MQTT_BROKER)
    client.subscribe(MQTT_TOPIC)
    return client


def main():
    client = create_client()
    client.loop_forever()


if __name__ == "__main__":
    main()
