import json
import os
import time
from datetime import datetime, timezone
import requests

# Channel to watch on Picarto.
CHANNEL_NAME = os.environ["CHANNEL_NAME"]

# How often to check the Picarto API.
CHECK_INTERVAL_SECONDS = 60

# Discord webhook used for live/offline alerts.
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# Small local file used to remember watcher state between restarts.
STATE_FILE = f"state_{CHANNEL_NAME}.json"

def utc_now():
    # Keep all timestamps in UTC so embeds and duration math stay consistent.
    return datetime.now(timezone.utc)

def format_duration(start_dt, end_dt):
    # Convert total runtime into a readable string for the offline alert.
    total_seconds = int((end_dt - start_dt).total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def looks_like_url(value):
    # Basic safety check before handing a string to Discord as an image URL.
    return isinstance(value, str) and value.startswith(("http://", "https://"))

def serialize_dt(value):
    # Store datetimes as ISO strings in the state file.
    if value is None:
        return None
    return value.isoformat()

def deserialize_dt(value):
    # Parse stored ISO timestamps back into datetime objects.
    if not value:
        return None
    return datetime.fromisoformat(value)

def load_state():
    # Load the last known watcher state from disk.
    if not os.path.exists(STATE_FILE):
        return {
            "was_live": False,
            "stream_start_dt": None,
            "last_live_status": None,
            "discord_message_id": None,
        }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        return {
            "was_live": bool(raw.get("was_live", False)),
            "stream_start_dt": deserialize_dt(raw.get("stream_start_dt")),
            "last_live_status": raw.get("last_live_status"),
            "discord_message_id": raw.get("discord_message_id"),
        }
    except Exception as e:
        print(f"[WARN] Failed to load state file: {e}")
        return {
            "was_live": False,
            "stream_start_dt": None,
            "last_live_status": None,
            "discord_message_id": None,
        }

def save_state(state):
    # Save watcher state to disk so restarts do not lose everything.
    payload = {
        "was_live": state["was_live"],
        "stream_start_dt": serialize_dt(state["stream_start_dt"]),
        "last_live_status": state["last_live_status"],
        "discord_message_id": state["discord_message_id"],
    }

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

def get_channel_status(channel_name):
    # Pull the current state of the Picarto channel.
    url = f"https://api.picarto.tv/api/v1/channel/name/{channel_name}"

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()

    # Normalize the bits we actually care about into one clean structure.
    return {
        "is_live": bool(data.get("online", False)),
        "title": data.get("title") or "Untitled stream",
        "category": data.get("category") or "Unknown category",
        "adult": data.get("adult", False),
        "viewers": data.get("viewers"),
        "avatar": data.get("avatar"),
        "thumbnails": data.get("thumbnails") or {},
        "channel_url": f"https://picarto.tv/{channel_name}",
    }

def get_webhook_edit_url(message_id):
    # Turn a normal webhook URL into the matching message-edit URL.
    return f"{DISCORD_WEBHOOK_URL}/messages/{message_id}"

def send_discord_embed(embed):
    # Send a new webhook message and return the created message ID.
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "PASTE_YOUR_WEBHOOK_URL_HERE":
        print("[WARN] Discord webhook URL not set. Skipping notification.")
        return None

    payload = {"embeds": [embed]}

    # wait=true makes Discord return the created message body.
    response = requests.post(
        DISCORD_WEBHOOK_URL,
        params={"wait": "true"},
        json=payload,
        timeout=15,
    )

    if not response.ok:
        print("[DEBUG] Discord response status:", response.status_code)
        print("[DEBUG] Discord response body:", response.text)

    response.raise_for_status()

    data = response.json()
    return data.get("id")

def edit_discord_embed(message_id, embed):
    # Edit an existing webhook message in place.
    if not message_id:
        print("[WARN] No Discord message ID available, cannot edit message.")
        return

    payload = {"embeds": [embed]}
    edit_url = get_webhook_edit_url(message_id)

    response = requests.patch(edit_url, json=payload, timeout=15)

    if not response.ok:
        print("[DEBUG] Discord edit response status:", response.status_code)
        print("[DEBUG] Discord edit response body:", response.text)

    response.raise_for_status()

def build_live_embed(status, start_dt):
    # Build the embed used when the stream first goes live.
    adult_text = "Yes" if status["adult"] else "No"
    viewers_text = str(status["viewers"]) if status["viewers"] is not None else "Unknown"

    embed = {
        "title": f"{CHANNEL_NAME} is LIVE on Picarto",
        "url": status["channel_url"],
        "description": str(status["title"]),
        "fields": [
            {"name": "Category", "value": str(status["category"]), "inline": True},
            {"name": "Viewers", "value": viewers_text, "inline": True},
            {"name": "Adult", "value": adult_text, "inline": True},
            {
                "name": "Started",
                "value": start_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "inline": False,
            },
        ],
        "timestamp": start_dt.isoformat(),
        "footer": {"text": "Picarto stream alert • Live"},
    }

    preview_url = None
    if isinstance(status["thumbnails"], dict):
        preview_url = (
            status["thumbnails"].get("web")
            or status["thumbnails"].get("mobile")
            or status["thumbnails"].get("thumbnail")
        )

    if looks_like_url(status.get("avatar")):
        embed["thumbnail"] = {"url": status["avatar"]}

    if looks_like_url(preview_url):
        embed["image"] = {"url": preview_url}

    return embed

def build_offline_embed(status, start_dt, end_dt):
    # Build the embed used when the stream ends.
    duration_text = format_duration(start_dt, end_dt)

    embed = {
        "title": f"{CHANNEL_NAME} is no longer streaming",
        "url": status["channel_url"],
        "description": str(status["title"]),
        "fields": [
            {"name": "Game Played", "value": str(status["category"]), "inline": False},
            {"name": "Streamed For", "value": duration_text, "inline": False},
        ],
        "timestamp": end_dt.isoformat(),
        "footer": {"text": "Picarto stream alert • Stopped streaming"},
    }

    if looks_like_url(status.get("avatar")):
        embed["thumbnail"] = {"url": status["avatar"]}

    return embed

def main():
    # Load any previous state so the watcher can survive restarts more gracefully.
    state = load_state()

    print(f"Watching Picarto channel: {CHANNEL_NAME}")

    while True:
        try:
            status = get_channel_status(CHANNEL_NAME)

            # Stream just went live.
            if status["is_live"] and not state["was_live"]:
                stream_start_dt = utc_now()

                state["was_live"] = True
                state["stream_start_dt"] = stream_start_dt
                state["last_live_status"] = status

                print(f"[ALERT] {CHANNEL_NAME} just went live at {stream_start_dt.isoformat()}")

                message_id = send_discord_embed(build_live_embed(status, stream_start_dt))
                state["discord_message_id"] = message_id

                save_state(state)

            # Stream just ended.
            elif not status["is_live"] and state["was_live"]:
                stream_end_dt = utc_now()

                print(f"[INFO] {CHANNEL_NAME} went offline at {stream_end_dt.isoformat()}")

                # Use the last live snapshot so title/category still exist in the offline embed.
                final_status = state["last_live_status"] if state["last_live_status"] is not None else status

                if state["discord_message_id"]:
                    edit_discord_embed(
                        state["discord_message_id"],
                        build_offline_embed(final_status, state["stream_start_dt"], stream_end_dt),
                    )
                else:
                    print("[WARN] Missing message ID, sending a new offline message instead.")
                    send_discord_embed(
                        build_offline_embed(final_status, state["stream_start_dt"], stream_end_dt)
                    )

                state["was_live"] = False
                state["stream_start_dt"] = None
                state["last_live_status"] = None
                state["discord_message_id"] = None

                save_state(state)

            # Stream is still live, so refresh cached live data.
            elif status["is_live"] and state["was_live"]:
                state["last_live_status"] = status
                save_state(state)
                print(f"[CHECK] {CHANNEL_NAME} is currently LIVE")

            # Stream is still offline.
            else:
                print(f"[CHECK] {CHANNEL_NAME} is currently offline")

        except requests.RequestException as e:
            print(f"[ERROR] Network/API problem: {e}")
        except Exception as e:
            print(f"[ERROR] Unexpected problem: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
