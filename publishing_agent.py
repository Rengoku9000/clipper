"""Publish new live clips and handle manual submissions."""

import logging
import sqlite3
import subprocess
import threading
import time
from queue import Empty, Queue
from uuid import uuid4

from publishing import (Accounts, BufferRejected, CloudinaryAuthorizationError, PLATFORMS, cloudinary_video,
                        create_buffer_post, host_buffer_video, publish, read_buffer_post)


class PublishingAgent:
    def __init__(self, url, db, clips, credential_store=None, filtering=None):
        self.accounts = Accounts(url, credential_store)
        self.db = db
        self.clips = clips
        self.filtering = filtering
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
        self.validate_clip(stats)
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
                if current and current["status"] not in {"failed", "held"}:
                    raise ValueError("A publish attempt already exists for this clip and destination. Check Publishing.")
                db.execute("""INSERT OR REPLACE INTO posts
                              (clip, platform, account, name, status, detail, updated, visibility)
                              VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
                           (*identifier, account["name"], "Waiting for the publishing agent", time.time(), visibility))
            self.jobs.put((self.publish_job, (identifier, path, fingerprint, title, description, visibility, made_for_kids)))

    @staticmethod
    def validate_clip(stats):
        if (stats["width"], stats["height"]) != (1080, 1920) or not 4 <= stats["duration"] <= 60:
            raise ValueError("Select a 4–60 second 1080 × 1920 clip for cross-platform compatibility.")

    def platform_settings(self):
        with self.db() as db:
            return {row["platform"]: bool(row["enabled"]) for row in
                    db.execute("SELECT platform, enabled FROM publishing_platforms")}

    def set_platform(self, platform, enabled):
        if platform not in PLATFORMS or not isinstance(enabled, bool):
            raise ValueError("Choose a publishing platform and its on/off setting.")
        with self.lock, self.db() as db:
            db.execute("UPDATE publishing_platforms SET enabled=? WHERE platform=?", (enabled, platform))

    def next_automatic_post(self):
        destinations = self.accounts.summary()["destinations"]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            enabled = {row["platform"] for row in db.execute("SELECT platform FROM publishing_platforms WHERE enabled=1")}
            clips = db.execute("""
                SELECT id, time, json_extract(data, '$.clip') AS clip FROM events
                WHERE id > (SELECT event_id FROM publishing_cursor WHERE id=1)
                  AND kind='ready' AND json_extract(data, '$.auto_publish')=1
                ORDER BY id LIMIT 100
            """).fetchall()
            for clip in clips:
                db.executemany("""INSERT OR IGNORE INTO posts
                    (clip, platform, account, name, status, detail, updated, visibility, automatic)
                    VALUES (?, ?, '', '', 'waiting', 'Connect one account for this platform.', ?, 'public', 1)
                """, [(clip["clip"], platform, clip["time"]) for platform in PLATFORMS if platform in enabled])
            if clips:
                db.execute("UPDATE publishing_cursor SET event_id=? WHERE id=1", (clips[-1]["id"],))

            queued = db.execute("""SELECT clip, platform, account FROM posts
                WHERE automatic=1 AND status='queued'
                  AND platform IN (SELECT platform FROM publishing_platforms WHERE enabled=1)
                ORDER BY updated LIMIT 1""").fetchone()
            if queued:
                return tuple(queued)

            # A manual submission for this platform already covers the automatic request.
            db.execute("""DELETE FROM posts AS waiting WHERE waiting.status='waiting' AND waiting.automatic=1
                AND EXISTS (SELECT 1 FROM posts AS submitted WHERE submitted.clip=waiting.clip
                    AND submitted.platform=waiting.platform AND submitted.account<>'' AND submitted.status<>'failed')""")
            eligible = {platform: accounts[0] for platform, accounts in destinations.items()
                        if len(accounts) == 1 and platform in enabled}
            if not eligible:
                return None
            slots = ",".join("?" for _ in eligible)
            waiting = db.execute(f"""SELECT clip, platform FROM posts
                WHERE automatic=1 AND status='waiting' AND platform IN ({slots})
                ORDER BY updated LIMIT 1""", tuple(eligible)).fetchone()
            if not waiting:
                return None
            clip, platform = waiting
            account = eligible[platform]
            identifier = clip, platform, account["id"]
            db.execute("""DELETE FROM posts WHERE clip=? AND platform=? AND account=? AND status='failed'""", identifier)
            db.execute("""UPDATE posts SET account=?, name=?, status='queued', detail='Waiting for the publishing agent'
                WHERE clip=? AND platform=? AND account=''""", (account["id"], account["name"], clip, platform))
            return identifier

    def publish_automatic(self, identifier):
        try:
            path = self.clips.clip_path(identifier[0])
            stats = self.clips.clip_stats(path)
            self.validate_clip(stats)
            copy = self.clips.caption_copy(path)
            title = (copy.get("title", "").strip() or f"{stats['channel']} live highlight")[:100]
            description = copy.get("description", "").strip()[:2000]
            stat = path.stat()
        except (ValueError, OSError, subprocess.SubprocessError, KeyError, IndexError):
            with self.db() as db:
                db.execute("""UPDATE posts SET status='failed', updated=?, detail=?
                    WHERE clip=? AND platform=? AND account=?""",
                           (time.time(), "The clip is missing or incompatible with automatic publishing.", *identifier))
            return
        self.publish_job(identifier, path, (stat.st_mtime_ns, stat.st_size), title, description, "public", False)

    def run(self):
        next_poll = time.monotonic() + 30
        active = False
        while True:
            try:
                job = self.jobs.get(timeout=0 if active else 1)
            except Empty:
                pass
            else:
                if job is None:
                    return
                operation, arguments = job
                operation(*arguments)
            active = False
            try:
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 30
                    self.poll_buffer()
                identifier = self.next_automatic_post()
                if identifier:
                    self.publish_automatic(identifier)
                    active = True
            except sqlite3.Error:
                logging.warning("The publishing agent could not access its queue. Retrying.")

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
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            post = db.execute("SELECT automatic FROM posts WHERE clip=? AND platform=? AND account=?", identifier).fetchone()
            if post and post["automatic"]:
                enabled = db.execute("SELECT enabled FROM publishing_platforms WHERE platform=?", (identifier[1],)).fetchone()
                if not enabled or not enabled["enabled"]:
                    return
                db.execute("UPDATE posts SET status='working', detail='Starting upload', updated=? WHERE clip=? AND platform=? AND account=?",
                           (time.time(), *identifier))
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
            record("Filtering Agent is screening this clip", status="screening")
            if self.filtering is None:
                record("Filtering Agent unavailable. Clip held for review.", status="held")
                return
            passed, reason = self.filtering.screen(path, title, description)
            if not passed:
                record(reason, status="held")
                return
            stat = path.stat()
            if (stat.st_mtime_ns, stat.st_size) != fingerprint:
                record("Clip changed during screening. Clip held for review.", status="held")
                return
            if self.platform_settings().get(identifier[1]) is False and post and post["automatic"]:
                record("Automatic publishing paused", status="queued")
                return
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
            elif buffer_asset and not isinstance(error, CloudinaryAuthorizationError) and not self.remove_buffer_asset(account, buffer_asset):
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
