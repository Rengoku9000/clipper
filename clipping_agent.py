"""Capture streams and produce clips for review."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from clipper import CLIP_PRESETS, DEFAULT_CLIP_PRESET, normalize_top_caption, youtube_video_id, youtube_watch_url


EVENT_PREFIX = "CLIPPER_EVENT "
CHANNEL = re.compile(r"[a-z0-9_]{1,25}", re.IGNORECASE)
YOUTUBE_RETRY_DELAY = 60


class ClippingAgent:
    def __init__(self, options, root, clips, db, platform="twitch"):
        self.options = options
        self.root = root
        self.clips = clips
        self.db = db
        self.platform = platform
        self.lock = threading.RLock()
        self.sessions = {}
        self.metadata = {}
        self.closed = False
        self.retry_after = 0

    def event(self, channel, kind, data=None):
        data = {**(data or {}), "platform": self.platform}
        with self.db() as db:
            return db.execute("INSERT INTO events (time, channel, kind, data) VALUES (?, ?, ?, ?)",
                              (time.time(), channel, kind, json.dumps(data or {}))).lastrowid

    def clip_path(self, relative):
        if not relative or not isinstance(relative, str):
            raise ValueError("Select a clip.")
        path = (self.clips / relative).resolve()
        if not path.is_relative_to(self.clips) or path.suffix.lower() != ".mp4":
            raise ValueError("Invalid clip path.")
        if any(part.startswith((".", "tmp")) for part in path.relative_to(self.clips).parts):
            raise ValueError("The clip is still rendering.")
        if not path.is_file():
            raise ValueError("Clip file was not found.")
        return path

    def clip_stats(self, path):
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            cached = self.metadata.get(path)
        if cached and cached[0] == key:
            return cached[1]
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode != 0:
            raise ValueError("Clip could not be read.")
        probe = json.loads(result.stdout)
        details = probe["streams"][0]
        details["duration"] = round(float(probe["format"]["duration"]), 2)
        details["name"] = path.name
        details["id"] = path.relative_to(self.clips).as_posix()
        details["size"] = stat.st_size
        details["time"] = stat.st_mtime
        name = re.match(r"(.+)-\d{8}-\d{6}-", path.name)
        details["channel"] = name[1] if name else "Local"
        with self.lock:
            self.metadata[path] = key, details
        return details

    def library(self):
        clips = []
        for path in self.clips.rglob("*.mp4"):
            try:
                path = self.clip_path(path.relative_to(self.clips).as_posix())
                item = self.clip_stats(path).copy()
                subs = path.with_suffix(".srt")
                item["cues"] = subs.read_text(encoding="utf-8").count(" --> ") if subs.exists() else 0
                item.update({f"suggested_{key}": value for key, value in self.caption_copy(path).items()})
                clips.append(item)
            except (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired):
                continue
        return sorted(clips, key=lambda clip: clip["time"], reverse=True)

    @staticmethod
    def caption_copy(path):
        try:
            copy = json.loads(path.with_suffix(".captions.json").read_text(encoding="utf-8"))
            if isinstance(copy, dict):
                return {key: copy[key] for key in ("title", "description") if isinstance(copy.get(key), str)}
        except (OSError, ValueError):
            pass
        return {}

    def start(self, channel, chat, rehearsal=False, top_caption="", clip_preset=DEFAULT_CLIP_PRESET):
        if self.platform == "youtube":
            if not isinstance(channel, str):
                raise ValueError("Enter a YouTube Live video link.")
            channel = youtube_video_id(channel.strip())
            source = youtube_watch_url(channel)
            destination = self.clips / "dashboard" / "~youtube" / channel
            if chat or rehearsal:
                raise ValueError("YouTube Live capture uses manual highlights.")
        else:
            if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
                raise ValueError("Enter a Twitch channel login.")
            source = channel = channel.lower()
            destination = self.clips / "dashboard" / channel
        top_caption = normalize_top_caption(top_caption)
        if not isinstance(clip_preset, str) or clip_preset not in CLIP_PRESETS:
            raise ValueError("Choose a clip length from the available presets.")
        _, before, after = CLIP_PRESETS[clip_preset]
        with self.lock:
            if self.closed:
                raise ValueError("The clipping agent is stopping.")
            if self.platform == "youtube" and time.monotonic() < self.retry_after:
                raise ValueError("YouTube rate-limited capture. Wait a minute before trying again.")
            if channel in self.sessions:
                raise ValueError("That channel is already running.")
            if chat and not all(os.getenv(key) for key in ("TWITCH_CLIENT_ID", "TWITCH_USER_TOKEN")):
                raise ValueError("Set the Twitch OAuth values before starting chat detection.")
            command = [sys.executable, "-u", str(self.root / "clipper.py"), "live", source,
                       "--quality", "720p60,720p,480p30,480p,best", "--output-dir", str(destination),
                       "--dashboard-events"]
            if self.options.model:
                command.extend(["--model", self.options.model])
            if top_caption:
                command.append(f"--top-caption={top_caption}")
            if chat:
                command.append("--chat")
            if rehearsal:
                if not self.options.rehearsal_input:
                    raise ValueError("Configure a local rehearsal recording first.")
                command.extend(["--input", str(self.options.rehearsal_input)])
                before, after = 5, 3
            command.extend(["--before", str(before), "--after", str(after)])
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("CLIPPER_META_", "CLIPPER_GOOGLE_"))}
            process = subprocess.Popen(
                command, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                env={**environment, "PYTHONIOENCODING": "utf-8"},
            )
            session = {
                "channel": channel, "platform": self.platform, "time": time.time(), "status": "Starting", "buffer": 0,
                "captured": 0, "ready": False, "waiting": 0, "rendering": 0, "highlights": 0,
                "chat": chat, "rehearsal": rehearsal, "logs": deque(maxlen=16),
                "process": process, "last_footage": None,
            }
            watcher = threading.Thread(target=self.watch, args=(session,), daemon=True)
            session["watcher"] = watcher
            self.sessions[channel] = session
            session["start_event"] = self.event(channel, "started", {"chat": chat, "rehearsal": rehearsal})
            watcher.start()

    def watch(self, session):
        channel, process = session["channel"], session["process"]
        with process.stdout:
            for line in process.stdout:
                line = line.strip()
                with self.lock:
                    if not line.startswith(EVENT_PREFIX):
                        if line:
                            session["logs"].append(re.sub(r"https?://[^\s)]+", "[URL]", line))
                            if self.platform == "youtube" and re.search(r"\b429 (?:Client Error|Too Many Requests)\b", line, re.IGNORECASE):
                                session["rate_limited"] = True
                                self.retry_after = time.monotonic() + YOUTUBE_RETRY_DELAY
                        continue
                    item = json.loads(line[len(EVENT_PREFIX):])
                    kind = item.pop("event")
                    if kind == "buffer":
                        session.update(buffer=item["seconds"], captured=item["captured"], ready=item["ready"])
                        session["last_footage"] = time.monotonic()
                        if session["status"] == "Stopping":
                            session["ready"] = False
                        else:
                            session["status"] = "Capturing"
                        continue
                    if kind == "marked":
                        session["waiting"] += 1
                        session["highlights"] += 1
                    if kind == "rendering":
                        session["waiting"] = max(0, session["waiting"] - 1)
                        session["rendering"] += 1
                    if kind in ("ready", "failed"):
                        stage = "waiting" if kind == "failed" and item.get("stage") == "waiting" else "rendering"
                        session[stage] = max(0, session[stage] - 1)
                    if kind == "ready":
                        item["clip"] = Path(item.pop("path")).relative_to(self.clips).as_posix()
                        item["auto_publish"] = not session["rehearsal"]
                    self.event(channel, kind, item)
        code = process.wait()
        process.stdin.close()
        with self.lock:
            stopping = session["status"] == "Stopping"
            self.sessions.pop(channel, None)
            detail = next(reversed(session["logs"]), "")
            if session.get("rate_limited") and not stopping and code != 0:
                detail = "YouTube rate-limited the capture (HTTP 429). Wait a minute before trying the live link again."
            self.event(channel, "stopped" if stopping or code == 0 else "capture_failed",
                       {"code": code, "message": detail})

    def signal(self, channel, stop=False):
        with self.lock:
            session = self.sessions.get(channel)
            if not session or session["process"].poll() is not None:
                raise ValueError("This capture is no longer running.")
            if session["status"] == "Stopping" or (not stop and not session["ready"]):
                raise ValueError("The capture is still starting or stopping.")
            if stop:
                session["status"] = "Stopping"
                session["ready"] = False
            session["process"].stdin.write("q\n" if stop else "\n")
            session["process"].stdin.flush()

    def active_sessions(self):
        with self.lock:
            return [
                {**{key: value for key, value in session.items()
                    if key not in ("process", "watcher", "logs", "last_footage", "start_event", "rate_limited")},
                 "logs": list(session["logs"])}
                for session in self.sessions.values()
            ]

    def close(self):
        with self.lock:
            self.closed = True
            sessions = list(self.sessions.values())
        for session in sessions:
            try:
                self.signal(session["channel"], stop=True)
            except (ValueError, OSError):
                pass
        for session in sessions:
            session["watcher"].join()
