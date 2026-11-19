"""Screen rendered clips locally before any publishing handoff."""

import hashlib
import re
import threading
import time


POLICY_VERSION = "nudity-1"
EXPOSED = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED",
           "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"}
SEXUAL_SCENE = re.compile(
    r"\b(strip\s*club|lap\s*danc(?:e|es|ing)|porn(?:ography|ographic)?|"
    r"sexual\s+(?:activity|intercourse)|sex\s+scene|naked|nudity|topless|"
    r"blow\s*job|hand\s*job|masturbat\w*)\b", re.IGNORECASE,
)


def text_flag(text):
    return bool(SEXUAL_SCENE.search(text))


class FilteringAgent:
    def __init__(self, clips, db):
        self.clips = clips
        self.db = db
        self.lock = threading.Lock()
        self.detector = None

    def summary(self):
        with self.db() as db:
            counts = {row["status"]: row["count"] for row in
                      db.execute("SELECT status, COUNT(*) AS count FROM screenings GROUP BY status")}
            recent = [dict(row) for row in db.execute(
                "SELECT clip, status, reason, updated FROM screenings ORDER BY updated DESC LIMIT 30")]
        return {"active": counts.get("scanning", 0), "passed": counts.get("passed", 0) + counts.get("approved", 0),
                "held": counts.get("held", 0) + counts.get("error", 0), "recent": recent}

    @staticmethod
    def signature(path, subtitles):
        digest = hashlib.sha256(POLICY_VERSION.encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0subtitles\0" + subtitles.encode("utf-8"))
        return digest.hexdigest()

    def save(self, clip, signature, status, reason):
        with self.db() as db:
            db.execute("""INSERT OR REPLACE INTO screenings (clip, signature, status, reason, updated)
                          VALUES (?, ?, ?, ?, ?)""", (clip, signature, status, reason, time.time()))

    def approve(self, clip, confirmed):
        if confirmed is not True:
            raise ValueError("Confirm that you reviewed the video before approving the visual flag.")
        path = self.clips.clip_path(clip)
        with self.lock:
            subtitles = path.with_suffix(".srt").read_text(encoding="utf-8") if path.with_suffix(".srt").exists() else ""
            signature = self.signature(path, subtitles)
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT * FROM screenings WHERE clip=? AND signature=?", (clip, signature)).fetchone()
                if not prior or prior["status"] not in {"held", "approved"}:
                    raise ValueError("This clip must have a completed screening hold before review approval.")
                if prior["reason"].startswith("Sexual-content language"):
                    raise ValueError("Edit the flagged caption or subtitles, then submit the clip for screening again.")
                db.execute("UPDATE screenings SET status='approved', reason=?, updated=? WHERE clip=? AND signature=?",
                           ("Visual flag approved after manual review.", time.time(), clip, signature))
                resumed = db.execute("""UPDATE posts SET status='queued', detail='Approved after review', updated=?
                    WHERE clip=? AND status='held' AND automatic=1
                      AND platform IN (SELECT platform FROM publishing_platforms WHERE enabled=1)""", (time.time(), clip)).rowcount
        return resumed

    def screen(self, path, title, description):
        clip = path.relative_to(self.clips.clips).as_posix()
        with self.lock:
            signature = ""
            try:
                subtitles = path.with_suffix(".srt").read_text(encoding="utf-8") if path.with_suffix(".srt").exists() else ""
                signature = self.signature(path, subtitles)
                if text_flag(f"{title}\n{description}\n{subtitles}"):
                    reason = "Sexual-content language in the title, caption, or subtitles."
                    self.save(clip, signature, "held", reason)
                    return False, reason
                with self.db() as db:
                    prior = db.execute("SELECT * FROM screenings WHERE clip=? AND signature=?", (clip, signature)).fetchone()
                if prior and prior["status"] in {"passed", "approved", "held"}:
                    return prior["status"] in {"passed", "approved"}, prior["reason"]
                self.save(clip, signature, "scanning", "Inspecting video frames for nudity.")
                reason = self.scan_frames(path)
                current_subtitles = path.with_suffix(".srt").read_text(encoding="utf-8") if path.with_suffix(".srt").exists() else ""
                if self.signature(path, current_subtitles) != signature:
                    raise ValueError("Clip changed during screening.")
                self.save(clip, signature, "held" if reason else "passed", reason or "Local visual and text screening passed.")
                return not reason, reason
            except Exception:
                reason = "Screening could not complete. Clip held for review."
                self.save(clip, signature, "error", reason)
                return False, reason

    def scan_frames(self, path):
        import cv2
        from nudenet import NudeDetector

        if self.detector is None:
            self.detector = NudeDetector(providers=["CPUExecutionProvider"])
        video = cv2.VideoCapture(str(path))
        started = time.monotonic()
        count = 0
        expected = video.get(cv2.CAP_PROP_FRAME_COUNT)
        fps = video.get(cv2.CAP_PROP_FPS)
        if not video.isOpened() or not 0 < fps <= 120:
            video.release()
            raise ValueError("Video could not be decoded.")
        try:
            while True:
                ok, frame = video.read()
                if not ok:
                    break
                if time.monotonic() - started > 300:
                    raise ValueError("Video screening timed out.")
                height = frame.shape[0]
                # The second view enlarges the foreground in the vertical stream layout.
                views = (frame, frame[height // 3:height * 2 // 3, :])
                for view in views:
                    for detection in self.detector.detect(view):
                        label, score = detection["class"], float(detection["score"])
                        if label in EXPOSED and score >= 0.4:
                            return f"Possible nudity at {count / fps:.1f}s ({label.lower().replace('_', ' ')})."
                        if label == "FEMALE_GENITALIA_COVERED" and score >= 0.7:
                            return f"Possible sexualized imagery at {count / fps:.1f}s."
                count += 1
                if count % 300 == 0:
                    clip = path.relative_to(self.clips.clips).as_posix()
                    with self.db() as db:
                        db.execute("UPDATE screenings SET reason=?, updated=? WHERE clip=? AND status='scanning'",
                                   (f"Inspecting video at {count / fps:.1f}s.", time.time(), clip))
        finally:
            video.release()
        if count == 0 or (expected > 0 and count < expected - 1):
            raise ValueError("Incomplete video decode.")
        return ""
