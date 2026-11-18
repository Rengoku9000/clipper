"""Run the clipper dashboard on this machine."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from clipper import CLIP_PRESETS, DEFAULT_CLIP_PRESET, normalize_top_caption
from publishing import Accounts, PLATFORMS, publish


ROOT = Path(__file__).resolve().parent
CLIPS = ROOT / "clips"
ASSETS = ROOT / "dashboard"
DATA = ROOT / ".dashboard"
EVENT_PREFIX = "CLIPPER_EVENT "
CHANNEL = re.compile(r"[a-z0-9_]{1,25}", re.IGNORECASE)
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}


class Dashboard:
    def __init__(self, options):
        self.options = options
        self.url = f"http://localhost:{options.port}"
        self.accounts = Accounts(self.url)
        self.lock = threading.RLock()
        self.capture_lock = threading.RLock()
        self.sessions = {}
        self.metadata = {}
        self.publish_threads = set()
        DATA.mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, time REAL NOT NULL, channel TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS posts (
                    clip TEXT NOT NULL, platform TEXT NOT NULL, account TEXT NOT NULL,
                    name TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                    remote TEXT, url TEXT, updated REAL NOT NULL,
                    PRIMARY KEY (clip, platform, account)
                );
                UPDATE posts SET status='check', detail='Dashboard stopped during upload. Check the platform for this post.'
                WHERE status='working';
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(DATA / "workflow.sqlite3", timeout=8)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, channel, kind, data=None):
        with self.db() as db:
            db.execute("INSERT INTO events (time, channel, kind, data) VALUES (?, ?, ?, ?)",
                       (time.time(), channel, kind, json.dumps(data or {})))

    def clip_path(self, relative):
        if not relative or not isinstance(relative, str):
            raise ValueError("Select a clip.")
        path = (CLIPS / relative).resolve()
        if not path.is_relative_to(CLIPS) or path.suffix.lower() != ".mp4":
            raise ValueError("Invalid clip path.")
        if any(part.startswith((".", "tmp")) for part in path.relative_to(CLIPS).parts):
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
        details["id"] = path.relative_to(CLIPS).as_posix()
        details["size"] = stat.st_size
        details["time"] = stat.st_mtime
        details["channel"] = path.name.split("-", 1)[0] if re.search(r"-\d{8}-\d{6}-", path.name) else "Local"
        with self.lock:
            self.metadata[path] = key, details
        return details

    def library(self):
        clips = []
        for path in CLIPS.rglob("*.mp4"):
            try:
                path = self.clip_path(path.relative_to(CLIPS).as_posix())
                item = self.clip_stats(path).copy()
                subs = path.with_suffix(".srt")
                item["cues"] = subs.read_text(encoding="utf-8").count(" --> ") if subs.exists() else 0
                clips.append(item)
            except (OSError, ValueError, IndexError, KeyError, subprocess.TimeoutExpired):
                continue
        return sorted(clips, key=lambda clip: clip["time"], reverse=True)

    def start(self, channel, chat, rehearsal=False, top_caption="", clip_preset=DEFAULT_CLIP_PRESET):
        if not isinstance(channel, str) or not CHANNEL.fullmatch(channel):
            raise ValueError("Enter a Twitch channel login.")
        channel = channel.lower()
        top_caption = normalize_top_caption(top_caption)
        if not isinstance(clip_preset, str) or clip_preset not in CLIP_PRESETS:
            raise ValueError("Choose a clip length from the available presets.")
        _, before, after = CLIP_PRESETS[clip_preset]
        with self.capture_lock:
            if channel in self.sessions:
                raise ValueError("That channel is already running.")
            if chat and not all(os.getenv(key) for key in ("TWITCH_CLIENT_ID", "TWITCH_USER_TOKEN")):
                raise ValueError("Set the Twitch OAuth values before starting chat detection.")
            command = [sys.executable, "-u", str(ROOT / "clipper.py"), "live", channel,
                       "--quality", "720p60,480p30,best", "--output-dir", str(CLIPS / "dashboard" / channel),
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
            process = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            session = {
                "channel": channel, "time": time.time(), "status": "Starting", "buffer": 0,
                "captured": 0, "ready": False, "waiting": 0, "rendering": 0, "highlights": 0,
                "chat": chat, "rehearsal": rehearsal, "logs": deque(maxlen=16),
                "process": process,
            }
            self.sessions[channel] = session
            self.event(channel, "started", {"chat": chat, "rehearsal": rehearsal})
            threading.Thread(target=self.watch, args=(session,), daemon=True).start()

    def watch(self, session):
        channel, process = session["channel"], session["process"]
        with process.stdout:
            for line in process.stdout:
                line = line.strip()
                with self.capture_lock:
                    if not line.startswith(EVENT_PREFIX):
                        if line:
                            session["logs"].append(line)
                        continue
                    item = json.loads(line[len(EVENT_PREFIX):])
                    kind = item.pop("event")
                    if kind == "buffer":
                        session.update(buffer=item["seconds"], captured=item["captured"], ready=item["ready"])
                        if session["status"] != "Stopping":
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
                        item["clip"] = Path(item.pop("path")).relative_to(CLIPS).as_posix()
                    self.event(channel, kind, item)
        code = process.wait()
        process.stdin.close()
        with self.capture_lock:
            stopping = session["status"] == "Stopping"
            self.sessions.pop(channel, None)
            detail = next(reversed(session["logs"]), "")
            self.event(channel, "stopped" if stopping or code == 0 else "capture_failed",
                       {"code": code, "message": detail})

    def signal(self, channel, stop=False):
        with self.capture_lock:
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

    def overview(self):
        library = self.library()
        with self.capture_lock:
            sessions = [
                {**{key: value for key, value in session.items() if key not in ("process", "logs")},
                 "logs": list(session["logs"])}
                for session in self.sessions.values()
            ]
        with self.db() as db:
            posts = [dict(row) for row in db.execute("SELECT * FROM posts ORDER BY updated DESC")]
            counts = {row["kind"]: row["count"] for row in
                      db.execute("SELECT kind, count(*) AS count FROM events GROUP BY kind")}
            recent = [dict(row) for row in db.execute(
                "SELECT time, channel, kind, data FROM events WHERE kind != 'chat' ORDER BY id DESC LIMIT 60"
            )]
            chat = [dict(row) for row in db.execute(
                "SELECT time, channel, kind, data FROM events WHERE kind = 'chat' ORDER BY id DESC LIMIT 240"
            )]
            latency = db.execute("SELECT AVG(json_extract(data, '$.latency')) FROM events WHERE kind='ready'").fetchone()[0]
        for event in recent + chat:
            event["data"] = json.loads(event["data"])
        return {
            "clips": library, "sessions": sessions, "posts": posts, "counts": counts,
            "activity": recent, "chat": chat, "average_latency": latency,
            "accounts": self.accounts.summary(), "rehearsal": bool(self.options.rehearsal_input),
            "chat_configured": bool(os.getenv("TWITCH_CLIENT_ID") and os.getenv("TWITCH_USER_TOKEN")),
            "top_caption_enabled": True,
            "clip_presets": [
                {"id": key, "label": label, "before": before, "after": after,
                 "default": key == DEFAULT_CLIP_PRESET}
                for key, (label, before, after) in CLIP_PRESETS.items()
            ],
        }

    def queue_post(self, data):
        path = self.clip_path(data.get("clip"))
        platform = data.get("platform")
        account_id = data.get("account")
        if platform not in PLATFORMS or not isinstance(account_id, str):
            raise ValueError("Select a publishing destination.")
        title = data.get("title", "").strip()
        description = data.get("description", "").strip()
        visibility = data.get("visibility", "private")
        made_for_kids = data.get("made_for_kids")
        if not title or len(title) > 100 or len(description) > 2000:
            raise ValueError("Enter a title up to 100 characters and a description up to 2,000 characters.")
        if visibility not in {"private", "unlisted", "public"} or data.get("confirmed") is not True:
            raise ValueError("Confirm the selected destination and permissions before publishing.")
        if platform == "youtube" and not isinstance(made_for_kids, bool):
            raise ValueError("Select the YouTube audience for this clip.")
        stats = self.clip_stats(path)
        if (stats["width"], stats["height"]) != (1080, 1920) or not 4 <= stats["duration"] <= 60:
            raise ValueError("Select a 4–60 second 1080 × 1920 clip for cross-platform compatibility.")
        account = self.accounts.get(platform, account_id)
        identifier = (path.relative_to(CLIPS).as_posix(), platform, account_id)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status FROM posts WHERE clip=? AND platform=? AND account=?", identifier).fetchone()
            if current and current["status"] != "failed":
                raise ValueError("A publish attempt already exists for this clip and destination. Check Publishing.")
            db.execute("INSERT OR REPLACE INTO posts VALUES (?, ?, ?, ?, 'working', ?, NULL, NULL, ?)",
                       (*identifier, account["name"], "Queued", time.time()))
        worker = threading.Thread(target=self.publish_worker,
                                  args=(identifier, account, path, title, description, visibility, made_for_kids), daemon=True)
        with self.lock:
            self.publish_threads.add(worker)
        worker.start()

    def publish_worker(self, identifier, account, path, title, description, visibility, made_for_kids):
        submitting = False

        def record(detail, remote=None, status="working", url=None):
            nonlocal submitting
            submitting = submitting or detail.startswith("Submitting")
            with self.db() as db:
                db.execute("""UPDATE posts SET status=?, detail=?, remote=COALESCE(?, remote), url=?, updated=?
                              WHERE clip=? AND platform=? AND account=?""",
                           (status, detail, remote, url, time.time(), *identifier))

        try:
            remote, url = publish(identifier[1], account, path, title, description, visibility, made_for_kids, record)
            status = "uploaded" if identifier[1] == "youtube" and visibility != "public" else "published"
            record(f"Uploaded ({visibility})" if status == "uploaded" else "Accepted by platform",
                   remote, status, url)
        except Exception as error:
            detail = str(error) if isinstance(error, RuntimeError) else "The upload could not be completed."
            if submitting:
                detail += " Check the platform for this clip before making another upload."
            record(detail, status="check" if submitting else "failed")
        finally:
            with self.lock:
                self.publish_threads.discard(threading.current_thread())


class Handler(BaseHTTPRequestHandler):
    @property
    def app(self):
        return self.server.app

    def log_message(self, *args):
        # OAuth callback query strings contain a one-time authorization code.
        pass

    def reply(self, status, data=b"", content="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in {"Cache-Control": "no-store", **(headers or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def json(self, status, data):
        self.reply(status, json.dumps(data).encode())

    def local_host(self):
        port = self.app.options.port
        return self.headers.get("Host") in {f"localhost:{port}", f"127.0.0.1:{port}"}

    def do_GET(self):
        target = urlsplit(self.path)
        path = target.path
        params = parse_qs(target.query)
        if path in ("/oauth/youtube", "/oauth/meta"):
            provider = path.split("/")[-1]
            configured = urlsplit(self.app.accounts.settings(provider)[2]).netloc
            if self.local_host() or self.headers.get("Host") == configured:
                return self.callback(provider, params)
        if not self.local_host():
            return self.json(HTTPStatus.FORBIDDEN, {"error": "Use the local dashboard URL."})
        try:
            if path in STATIC:
                filename, content = STATIC[path]
                self.reply(HTTPStatus.OK, (ASSETS / filename).read_bytes(), content + "; charset=utf-8")
            elif path == "/api/overview":
                self.json(HTTPStatus.OK, self.app.overview())
            elif path in ("/clip", "/captions", "/thumbnail"):
                video = self.app.clip_path(params.get("id", [""])[0])
                if path == "/clip":
                    self.video(video)
                elif path == "/captions":
                    self.reply(HTTPStatus.OK, video.with_suffix(".srt").read_bytes(),
                               "text/plain; charset=utf-8", {"Content-Disposition": "attachment; filename=clip.srt"})
                else:
                    self.thumbnail(video)
            else:
                self.json(HTTPStatus.NOT_FOUND, {"error": "Route not found."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, OSError, subprocess.SubprocessError):
            self.json(HTTPStatus.NOT_FOUND, {"error": "Requested file or data is unavailable."})

    def do_POST(self):
        port = self.app.options.port
        if (not self.local_host() or self.headers.get("Origin") not in
                {self.app.url, f"http://127.0.0.1:{port}"}
                or self.headers.get_content_type() != "application/json"):
            return self.json(HTTPStatus.FORBIDDEN, {"error": "Use the local dashboard to make changes."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16_384:
                raise ValueError("Invalid request size.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Invalid request.")
            response = {"ok": True}
            if self.path == "/api/start":
                self.app.start(data.get("channel"), data.get("chat") is True,
                               data.get("rehearsal") is True, data.get("top_caption", ""),
                               data.get("clip_preset", DEFAULT_CLIP_PRESET))
            elif self.path in ("/api/mark", "/api/stop"):
                self.app.signal(data.get("channel"), self.path == "/api/stop")
            elif self.path == "/api/connect":
                response["url"] = self.app.accounts.begin(data.get("provider"))
            elif self.path == "/api/configure-meta":
                self.app.accounts.configure_meta(data.get("identifier"), data.get("secret"), data.get("config_id"))
            elif self.path == "/api/disconnect":
                self.app.accounts.disconnect(data.get("platform"))
            elif self.path == "/api/publish":
                self.app.queue_post(data)
            else:
                return self.json(HTTPStatus.NOT_FOUND, {"error": "Route not found."})
            self.json(HTTPStatus.OK, response)
        except (ValueError, RuntimeError, OSError, TypeError, AttributeError) as error:
            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "The request could not be completed."
            self.json(HTTPStatus.BAD_REQUEST, {"error": message})

    def callback(self, provider, params):
        try:
            self.app.accounts.finish(provider, params.get("state", [""])[0], params.get("code", [""])[0])
        except (ValueError, RuntimeError, KeyError) as error:
            self.app.accounts.notice = str(error) if not isinstance(error, KeyError) else "The provider did not return an eligible account."
        self.reply(HTTPStatus.SEE_OTHER, headers={"Location": f"{self.app.url}/#accounts"})

    def thumbnail(self, video):
        thumbnail = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", "1", "-i", str(video),
             "-vf", "scale=162:-2", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=8, check=True,
        ).stdout
        self.reply(HTTPStatus.OK, thumbnail, "image/jpeg", {"Cache-Control": "private, max-age=3600"})

    def video(self, path):
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if match and match[1]:
                start, end = int(match[1]), min(end, int(match[2])) if match[2] else end
            elif match and match[2]:
                start = max(0, size - int(match[2]))
            else:
                start = size
            if start >= size or start > end:
                return self.reply(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                                  headers={"Content-Range": f"bytes */{size}"})
        self.send_response(HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if range_header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as stream:
            if not range_header:
                shutil.copyfileobj(stream, self.wfile)
                return
            stream.seek(start)
            remaining = end - start + 1
            while remaining and (chunk := stream.read(min(remaining, 256 * 1024))):
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    local_model = ROOT / ".venv-cache" / "whisper-base"
    parser.add_argument("--model", default=str(local_model) if (local_model / "model.bin").exists() else "base")
    parser.add_argument("--rehearsal-input", type=Path)
    options = parser.parse_args()
    if options.rehearsal_input:
        options.rehearsal_input = options.rehearsal_input.resolve()
        if not options.rehearsal_input.is_file():
            parser.error("Rehearsal input must be an existing file.")
    app = Dashboard(options)
    with ThreadingHTTPServer(("127.0.0.1", options.port), Handler) as server:
        server.app = app
        print(f"Clipper dashboard: {app.url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            with app.capture_lock:
                active = list(app.sessions.values())
            for session in active:
                if session["status"] != "Stopping":
                    try:
                        app.signal(session["channel"], stop=True)
                    except (ValueError, OSError):
                        pass
            for session in active:
                session["process"].wait()
            with app.lock:
                posts = tuple(app.publish_threads)
            for worker in posts:
                worker.join()


if __name__ == "__main__":
    main()
