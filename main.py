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

def utc_now():
    # Keep all timestamps in UTC so embeds and duration math stay consistent.
    return datetime.now(timezone.utc)

def utc_now_iso():
    # Small helper in case an ISO UTC timestamp is needed elsewhere.
    return utc_now().isoformat()

def format_duration_minutes(start_dt, end_dt):
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

def send_discord_embed(embed):
    # Skip sending if the webhook was never configured.
    if not DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL == "PASTE_YOUR_WEBHOOK_URL_HERE":
        print("[WARN] Discord webhook URL not set. Skipping notification.")
        return

    payload = {"embeds": [embed]}
    response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)

    # If Discord rejects the payload, dump some debug info before raising.
    if not response.ok:
        print("[DEBUG] Discord response status:", response.status_code)
        print("[DEBUG] Discord response body:", response.text)

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
        "footer": {"text": "Picarto stream alert"},
    }

    # Prefer a stream preview image if Picarto gives us one.
    preview_url = None
    if isinstance(status["thumbnails"], dict):
        preview_url = (
            status["thumbnails"].get("web")
            or status["thumbnails"].get("mobile")
            or status["thumbnails"].get("thumbnail")
        )

    # Add the channel avatar as a thumbnail when available.
    if looks_like_url(status.get("avatar")):
        embed["thumbnail"] = {"url": status["avatar"]}

    # Add the live preview image to make the alert more useful at a glance.
    if looks_like_url(preview_url):
        embed["image"] = {"url": preview_url}

    return embed

def build_offline_embed(status, start_dt, end_dt):
    # Build the embed used when the stream ends.
    duration_text = format_duration_minutes(start_dt, end_dt)

    embed = {
        "title": f"{CHANNEL_NAME} went offline",
        "url": status["channel_url"],
        "description": str(status["title"]),
        "fields": [
            {
                "name": "Started",
                "value": start_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "inline": False,
            },
            {
                "name": "Ended",
                "value": end_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "inline": False,
            },
            {"name": "Duration", "value": duration_text, "inline": False},
        ],
        "timestamp": end_dt.isoformat(),
        "footer": {"text": "Picarto stream alert"},
    }

    # Keep the avatar on the offline embed too so it still looks tied to the channel.
    if looks_like_url(status.get("avatar")):
        embed["thumbnail"] = {"url": status["avatar"]}

    return embed

def main():
    # Tracks whether the last known state was live or offline.
    was_live = False

    # Stores when the current stream started so runtime can be calculated later.
    stream_start_dt = None

    # Keeps the most recent live status around in case Picarto strips fields once offline.
    last_live_status = None

    print(f"Watching Picarto channel: {CHANNEL_NAME}")

    while True:
        try:
            status = get_channel_status(CHANNEL_NAME)

            # Stream just went live.
            if status["is_live"] and not was_live:
                stream_start_dt = utc_now()
                was_live = True
                last_live_status = status

                print(f"[ALERT] {CHANNEL_NAME} just went live at {stream_start_dt.isoformat()}")
                send_discord_embed(build_live_embed(status, stream_start_dt))

            # Stream just ended.
            elif not status["is_live"] and was_live:
                stream_end_dt = utc_now()

                print(f"[INFO] {CHANNEL_NAME} went offline at {stream_end_dt.isoformat()}")

                # Use the last live snapshot so title/category still exist in the offline alert.
                final_status = last_live_status if last_live_status is not None else status
                send_discord_embed(build_offline_embed(final_status, stream_start_dt, stream_end_dt))

                was_live = False
                stream_start_dt = None
                last_live_status = None

            # Stream is still live, so just refresh the cached live data.
            elif status["is_live"] and was_live:
                last_live_status = status
                print(f"[CHECK] {CHANNEL_NAME} is currently LIVE")

            # Stream is still offline.
            else:
                print(f"[CHECK] {CHANNEL_NAME} is currently offline")

        except requests.RequestException as e:
            # Covers API failures, timeouts, connection issues, etc.
            print(f"[ERROR] Network/API problem: {e}")
        except Exception as e:
            # Catch-all so the watcher doesn't die on an unexpected edge case.
            print(f"[ERROR] Unexpected problem: {e}")

        # Wait before polling again.
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
