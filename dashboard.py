"""Run the clipper dashboard on this machine."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from clipper import CLIP_PRESETS, DEFAULT_CLIP_PRESET
from clipping_agent import ClippingAgent
from credential_store import CredentialStore
from publishing_agent import PublishingAgent
from stream_monitoring_agent import StreamMonitoringAgent
from twitch_chat_auth import configured as twitch_chat_configured


ROOT = Path(__file__).resolve().parent
CLIPS = ROOT / "clips"
ASSETS = ROOT / "dashboard"
DATA = ROOT / ".dashboard"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css")}


class Dashboard:
    def __init__(self, options):
        self.options = options
        self.url = f"http://localhost:{options.port}"
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
                CREATE TABLE IF NOT EXISTS publishing_cursor (
                    id INTEGER PRIMARY KEY CHECK (id = 1), event_id INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO publishing_cursor SELECT 1, COALESCE(MAX(id), 0) FROM events;
                UPDATE posts SET status='check', detail='Dashboard stopped during upload. Check the platform for this post.'
                WHERE status='working';
            """)
            fields = {column["name"] for column in db.execute("PRAGMA table_info(posts)")}
            if "buffer_asset" not in fields:
                db.execute("ALTER TABLE posts ADD COLUMN buffer_asset TEXT")
            if "visibility" not in fields:
                db.execute("ALTER TABLE posts ADD COLUMN visibility TEXT")
            if "automatic" not in fields:
                db.execute("ALTER TABLE posts ADD COLUMN automatic INTEGER NOT NULL DEFAULT 0")
            db.execute("""UPDATE posts SET status='failed',
                          detail='Dashboard stopped before upload. Review and submit the clip again.'
                          WHERE status='queued' AND automatic=0""")
        self.clipping = ClippingAgent(options, ROOT, CLIPS, self.db)
        self.youtube_clipping = ClippingAgent(options, ROOT, CLIPS, self.db, platform="youtube")
        self.monitoring = StreamMonitoringAgent(self.clipping, self.db)
        self.youtube_monitoring = StreamMonitoringAgent(self.youtube_clipping, self.db)
        credentials = CredentialStore(DATA / "publishing-credentials.dpapi") if os.name == "nt" else None
        self.publisher = PublishingAgent(self.url, self.db, self.clipping, credentials)

    @contextmanager
    def db(self):
        db = sqlite3.connect(DATA / "workflow.sqlite3", timeout=8)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def overview(self):
        library = self.clipping.library()
        with self.clipping.lock:
            sessions = self.clipping.active_sessions()
            monitoring = self.monitoring.snapshot()
        with self.youtube_clipping.lock:
            youtube_sessions = self.youtube_clipping.active_sessions()
            youtube_monitoring = self.youtube_monitoring.snapshot()
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
            "clips": library, "sessions": sessions + youtube_sessions, "posts": posts, "counts": counts,
            "agents": {"clipping": {"active": len(sessions)}, "monitoring": monitoring,
                       "youtube_clipping": {"active": len(youtube_sessions)}, "youtube_monitoring": youtube_monitoring,
                       "publishing": {"online": self.publisher.worker.is_alive(),
                                      "queued": sum(post["status"] == "queued" for post in posts),
                                      "active": sum(post["status"] == "working" for post in posts)}},
            "activity": recent, "chat": chat, "average_latency": latency,
            "accounts": self.publisher.accounts.summary(), "rehearsal": bool(self.options.rehearsal_input),
            "chat_configured": twitch_chat_configured(),
            "top_caption_enabled": True,
            "clip_presets": [
                {"id": key, "label": label, "before": before, "after": after,
                 "default": key == DEFAULT_CLIP_PRESET}
                for key, (label, before, after) in CLIP_PRESETS.items()
            ],
        }

    def clipper(self, platform):
        if platform == "twitch":
            return self.clipping
        if platform == "youtube":
            return self.youtube_clipping
        raise ValueError("Choose Twitch or YouTube Live.")

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
            configured = urlsplit(self.app.publisher.accounts.settings(provider)[2]).netloc
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
                video = self.app.clipping.clip_path(params.get("id", [""])[0])
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
                self.app.clipper(data.get("platform", "twitch")).start(data.get("channel"), data.get("chat") is True,
                               data.get("rehearsal") is True, data.get("top_caption", ""),
                               data.get("clip_preset", DEFAULT_CLIP_PRESET))
            elif self.path in ("/api/mark", "/api/stop"):
                self.app.clipper(data.get("platform", "twitch")).signal(data.get("channel"), self.path == "/api/stop")
            elif self.path == "/api/connect":
                response["url"] = self.app.publisher.accounts.begin(data.get("provider"))
            elif self.path == "/api/configure-meta":
                self.app.publisher.accounts.configure_meta(data.get("identifier"), data.get("secret"), data.get("config_id"))
            elif self.path == "/api/configure-youtube":
                self.app.publisher.accounts.configure_youtube(data.get("identifier"), data.get("secret"))
            elif self.path == "/api/configure-buffer":
                self.app.publisher.accounts.configure_buffer(data.get("buffer_key"), data.get("cloud_name"),
                                                             data.get("cloud_key"), data.get("cloud_secret"))
            elif self.path == "/api/refresh-buffer":
                self.app.publisher.refresh_buffer()
            elif self.path == "/api/disconnect":
                self.app.publisher.accounts.disconnect(data.get("platform"))
            elif self.path == "/api/publish":
                self.app.publisher.queue_post(data)
            else:
                return self.json(HTTPStatus.NOT_FOUND, {"error": "Route not found."})
            self.json(HTTPStatus.OK, response)
        except (ValueError, RuntimeError, OSError, TypeError, AttributeError) as error:
            message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "The request could not be completed."
            self.json(HTTPStatus.BAD_REQUEST, {"error": message})

    def callback(self, provider, params):
        try:
            self.app.publisher.accounts.finish(provider, params.get("state", [""])[0], params.get("code", [""])[0])
        except (ValueError, RuntimeError, KeyError) as error:
            self.app.publisher.accounts.notice = str(error) if not isinstance(error, KeyError) else "The provider did not return an eligible account."
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
    with ThreadingHTTPServer(("127.0.0.1", options.port), Handler) as server:
        app = Dashboard(options)
        server.app = app
        print(f"Clipper dashboard: {app.url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            app.clipping.close()
            app.youtube_clipping.close()
            app.publisher.close()


if __name__ == "__main__":
    main()
