"""Take reviewed clips and publish them to approved destinations."""

import threading
import time
from queue import Empty, Queue
from uuid import uuid4

from publishing import (Accounts, BufferRejected, PLATFORMS, cloudinary_video,
                        create_buffer_post, host_buffer_video, publish, read_buffer_post)


class PublishingAgent:
    def __init__(self, url, db, clips):
        self.accounts = Accounts(url)
        self.db = db
        self.clips = clips
        self.lock = threading.Lock()
        self.jobs = Queue()
        self.worker = threading.Thread(target=self.run, name="publishing-agent", daemon=True)
        self.closed = False
        self.worker.start()

    def queue_post(self, data):
        path = self.clips.clip_path(data.get("clip"))
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
        stats = self.clips.clip_stats(path)
        if (stats["width"], stats["height"]) != (1080, 1920) or not 4 <= stats["duration"] <= 60:
            raise ValueError("Select a 4–60 second 1080 × 1920 clip for cross-platform compatibility.")
        account = self.accounts.get(platform, account_id)
        identifier = (stats["id"], platform, account_id)
        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            if self.closed:
                raise ValueError("The publishing agent is stopping.")
            if not self.worker.is_alive():
                raise ValueError("The publishing agent is offline.")
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status FROM posts WHERE clip=? AND platform=? AND account=?",
                                     identifier).fetchone()
                if current and current["status"] != "failed":
                    raise ValueError("A publish attempt already exists for this clip and destination. Check Publishing.")
                db.execute("""INSERT OR REPLACE INTO posts
                              (clip, platform, account, name, status, detail, updated, visibility)
                              VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                           (*identifier, account["name"], "Waiting for the publishing agent", time.time(), visibility))
            self.jobs.put((self.publish_job, (identifier, path, fingerprint, title, description, visibility, made_for_kids)))

    def run(self):
        while True:
            try:
                job = self.jobs.get(timeout=30)
            except Empty:
                self.poll_buffer()
                continue
            if job is None:
                return
            operation, arguments = job
            operation(*arguments)

    def refresh_buffer(self):
        with self.lock:
            if self.closed or not self.worker.is_alive():
                raise ValueError("The publishing agent is unavailable.")
            self.jobs.put((self.poll_buffer, (True,)))

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.jobs.put(None)
        self.worker.join()

    def publish_job(self, identifier, path, fingerprint, title, description, visibility, made_for_kids):
        submitting = False
        buffer_asset = None

        def record(detail, remote=None, status="working", url=None, asset=None):
            nonlocal submitting
            submitting = submitting or detail.startswith("Submitting")
            with self.db() as db:
                db.execute("""UPDATE posts SET status=?, detail=?, remote=COALESCE(?, remote), url=?, updated=?,
                              buffer_asset=COALESCE(?, buffer_asset)
                              WHERE clip=? AND platform=? AND account=?""",
                           (status, detail, remote, url, time.time(), asset, *identifier))

        try:
            stat = path.stat()
            if (stat.st_mtime_ns, stat.st_size) != fingerprint:
                raise ValueError("The clip changed after approval. Review it again before publishing.")
            account = self.accounts.get(identifier[1], identifier[2])
            record("Starting upload")
            if account.get("via") == "buffer":
                buffer_asset = f"{account['cloud_name']}/clipper-{uuid4().hex}"
                record("Hosting approved clip", asset=buffer_asset)
                media_url = host_buffer_video(account, buffer_asset.split("/", 1)[1], path)
                record("Submitting to Buffer")
                try:
                    post = create_buffer_post(account, media_url, title, description, visibility, made_for_kids)
                except BufferRejected:
                    submitting = False
                    raise
                record("Accepted by Buffer. Waiting for YouTube.", post["id"], "buffer_pending")
                self.finish_buffer_post(identifier, account, buffer_asset, visibility, post)
                return
            remote, url = publish(identifier[1], account, path, title, description, visibility, made_for_kids, record)
            status = "uploaded" if identifier[1] == "youtube" and visibility != "public" else "published"
            record(f"Uploaded ({visibility})" if status == "uploaded" else "Accepted by platform",
                   remote, status, url)
        except Exception as error:
            detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else "The upload could not be completed."
            if submitting:
                detail += " Check the platform for this clip before making another upload."
            elif buffer_asset and not self.remove_buffer_asset(account, buffer_asset):
                detail += " Check the Cloudinary asset before retrying."
            record(detail, status="check" if submitting else "failed")

    @staticmethod
    def remove_buffer_asset(account, asset):
        cloud, identifier = asset.split("/", 1)
        if cloud != account["cloud_name"] or not identifier.startswith("clipper-"):
            return False
        try:
            return cloudinary_video(account, "destroy", identifier).get("result") in {"ok", "not found"}
        except (RuntimeError, ValueError):
            return False

    def finish_buffer_post(self, identifier, account, asset, visibility, post):
        status = post["status"]
        if status == "sent":
            outcome = "published" if visibility == "public" else "uploaded"
            detail = f"Confirmed by Buffer ({visibility})"
            if not self.remove_buffer_asset(account, asset):
                detail += ". Cloudinary cleanup needs attention."
        elif status in {"error", "draft", "needs_approval"}:
            outcome, detail = "check", f"Buffer status: {status.replace('_', ' ')}. Review the existing post in Buffer."
        else:
            return
        with self.db() as db:
            db.execute("""UPDATE posts SET status=?, detail=?, url=?, updated=?
                          WHERE clip=? AND platform=? AND account=?""",
                       (outcome, detail, post.get("externalLink"), time.time(), *identifier))

    def poll_buffer(self, manual=False):
        with self.db() as db:
            posts = db.execute("""SELECT * FROM posts WHERE buffer_asset IS NOT NULL AND remote IS NOT NULL
                                  AND (status='buffer_pending' OR (? AND status='check'))
                                  AND (? OR updated > ?) ORDER BY updated LIMIT 10""",
                               (manual, manual, time.time() - 600)).fetchall()
        for post in posts:
            identifier = (post["clip"], post["platform"], post["account"])
            try:
                account = self.accounts.get(post["platform"], post["account"])
                if account.get("via") != "buffer":
                    continue
                current = read_buffer_post(account, post["remote"])
                self.finish_buffer_post(identifier, account, post["buffer_asset"], post["visibility"], current)
            except (KeyError, ValueError, RuntimeError):
                continue
