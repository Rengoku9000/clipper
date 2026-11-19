"""Read public YouTube live chat for highlight detection."""

import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests

from credential_store import CredentialStore


API = "https://www.googleapis.com/youtube/v3"
STORE = CredentialStore(Path(__file__).resolve().parent / ".dashboard" / "youtube-chat.dpapi")


def api_key():
    key = os.getenv("YOUTUBE_CHAT_API_KEY")
    if key:
        return key
    if os.name == "nt":
        try:
            return STORE.load().get("api_key")
        except (OSError, ValueError):
            pass
    return None


def request(key, resource, **params):
    try:
        response = requests.get(f"{API}/{resource}", params=params,
                                headers={"X-Goog-Api-Key": key}, timeout=12)
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("YouTube chat connection failed. Check your connection and retry.") from None
    if not response.ok:
        reason = next(iter(payload.get("error", {}).get("errors", [])), {}).get("reason")
        if reason in {"quotaExceeded", "dailyLimitExceeded"}:
            raise RuntimeError("YouTube chat quota is exhausted. Chat detection stops until quota is available.")
        if reason in {"liveChatEnded", "liveChatDisabled", "liveChatNotFound"}:
            raise RuntimeError("YouTube live chat ended, is disabled, or is unavailable.")
        raise RuntimeError("YouTube rejected chat access. Check the API key, its restrictions, and that YouTube Data API v3 is enabled.")
    return payload


def configure(key):
    if not isinstance(key, str) or not re.fullmatch(r"AIza[\w-]{35}", key.strip()):
        raise ValueError("Enter a YouTube Data API key from your Google Cloud project.")
    key = key.strip()
    request(key, "videos", part="id", id="JDf1z0OUC2E")
    if os.name != "nt":
        raise ValueError("Set YOUTUBE_CHAT_API_KEY in the server environment on this system.")
    STORE.save({"api_key": key})


def read_chat(video_id, events, stop):
    key = api_key()
    if not key:
        print("Configure YouTube chat in Accounts before enabling detection.", file=sys.stderr)
        return
    try:
        result = request(key, "videos", part="liveStreamingDetails", id=video_id)
        videos = result.get("items", [])
        chat_id = videos[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if videos else None
        if not chat_id:
            raise RuntimeError("This YouTube video has no active live chat.")
        params = {"liveChatId": chat_id, "part": "id,snippet", "maxResults": 2000}
        seen = set()
        recent = deque()
        started = datetime.now(timezone.utc)
        first = True
        while not stop.is_set():
            payload = request(key, "liveChat/messages", **params)
            if first:
                events.put(("chat_reset", None))
                print(f"Reading YouTube chat for {video_id}.", flush=True)
            for item in payload.get("items", []):
                identifier = item["id"]
                if identifier in seen:
                    continue
                seen.add(identifier)
                recent.append(identifier)
                if len(recent) > 10000:
                    seen.remove(recent.popleft())
                snippet = item["snippet"]
                published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
                if not first and published >= started and snippet.get("type") in {"textMessageEvent", "superChatEvent", "superStickerEvent"}:
                    author = snippet.get("authorChannelId")
                    if author:
                        events.put(("chat", author))
            first = False
            if payload.get("offlineAt") or not payload.get("nextPageToken"):
                break
            params["pageToken"] = payload["nextPageToken"]
            if stop.wait(max(5, payload.get("pollingIntervalMillis", 5000) / 1000)):
                break
    except (RuntimeError, KeyError, TypeError, ValueError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "YouTube returned unexpected chat data."
        print(message, file=sys.stderr, flush=True)
    finally:
        events.put(("chat_reset", None))
