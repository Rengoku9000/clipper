"""Manage a past-video clipping job from the local dashboard."""

import math
import subprocess
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock, Thread
from uuid import uuid4

from clipper import youtube_video_id, youtube_watch_url
from vod_agent import download_replay, prepare_edits, probe, render_batch


BUSY = {"downloading", "analyzing", "rendering"}
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts"}


class VodWorkflow:
    def __init__(self, data, clips, model):
        self.data = data
        self.clips = clips
        self.model = model
        self.lock = RLock()
        self.job = None
        self.workspace = None
        self.source = None
        self.media = None

    def snapshot(self):
        with self.lock:
            return self.job.copy() if self.job else None

    def progress(self, phase, detail):
        with self.lock:
            self.job.update(phase=phase, detail=detail)

    def prepare(self, data):
        value = data.get("source")
        if not isinstance(value, str) or not value.strip() or len(value) > 4096:
            raise ValueError("Enter a YouTube URL or a local recording path.")
        value = value.strip()
        url, recording = "", None
        if value.startswith(("https://", "http://")):
            try:
                url = youtube_watch_url(youtube_video_id(value))
            except ValueError as error:
                raise ValueError("Enter a YouTube video URL.") from error
        else:
            recording = Path(value.strip('"')).resolve()
            if recording.suffix.lower() not in MEDIA_SUFFIXES or not recording.is_file():
                raise ValueError("Choose an existing local video file or enter a YouTube URL.")

        with self.lock:
            if self.job and self.job["phase"] in BUSY:
                raise ValueError("A past-video job is already running.")
            if self.workspace:
                self.workspace.cleanup()
            self.workspace = TemporaryDirectory(prefix="vod-source-", dir=self.data) if url else None
            self.source, self.media = None, None
            self.job = {"id": uuid4().hex, "phase": "downloading", "detail": "Opening the video…",
                        "title": "", "error": "", "url": url, "clips": []}
            Thread(target=self._prepare, args=(url, recording), daemon=True, name="vod-prepare").start()
            return self.snapshot()

    def _prepare(self, url, recording):
        try:
            if url:
                source, title = download_replay(url, Path(self.workspace.name), allow_upload=True,
                                                progress=self.progress)
            else:
                source, title = recording, recording.stem
            media = probe(source)
            with self.lock:
                self.source, self.media = source, media
                width, height, duration, _ = media
                self.job.update(phase="crop", detail="Choose the facecam and gameplay areas.",
                                title=title, width=width, height=height, duration=duration)
        except (ValueError, RuntimeError, OSError, ImportError, subprocess.SubprocessError) as error:
            with self.lock:
                self.job.update(phase="failed", detail="Could not open the video.", error=self._error(error))

    def frame(self, job_id, seconds):
        with self.lock:
            if not self.job or self.job["id"] != job_id or self.job["phase"] != "crop":
                raise ValueError("Open a video before previewing a frame.")
            second = float(seconds)
            if not math.isfinite(second) or not 0 <= second < self.media[2]:
                raise ValueError("Choose a preview time within the video.")
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-ss", str(second), "-i", str(self.source),
                "-vf", "scale=w='min(960,iw)':h=-2", "-frames:v", "1", "-f", "image2pipe",
                "-vcodec", "mjpeg", "pipe:1",
            ], capture_output=True, timeout=15, check=True)
            if not result.stdout:
                raise ValueError("No frame at that time.")
            return result.stdout

    def start(self, data):
        with self.lock:
            if not self.job or self.job["id"] != data.get("id") or self.job["phase"] != "crop":
                raise ValueError("Open a video and set its crops before generating drafts.")
            facecam = self._crop(data.get("facecam"), "facecam")
            gameplay = self._crop(data.get("gameplay"), "gameplay") if data.get("gameplay") is not None else None
            count, duration, game = data.get("count"), data.get("duration"), data.get("game", "")
            if type(count) is not int or not 1 <= count <= 10 or type(duration) is not int or not 10 <= duration <= 60:
                raise ValueError("Choose 1–10 clips and a target length of 10–60 seconds.")
            if not isinstance(game, str) or len(game) > 80:
                raise ValueError("Keep the game name within 80 characters.")
            ranges = data.get("ranges", [])
            if not isinstance(ranges, list) or len(ranges) > 10 or any(
                    not isinstance(pair, list) or len(pair) != 2 or
                    any(type(point) not in (float, int) for point in pair) for pair in ranges):
                raise ValueError("Enter up to ten ranges as start and end seconds.")
            crops, clips = prepare_edits(self.media, facecam, gameplay, ranges)
            options = Namespace(model=self.model, count=count, duration=duration, game=game.strip(),
                                output_dir=self.clips / "vod-drafts")
            self.job.update(phase="analyzing", detail="Starting highlight analysis…", error="")
            Thread(target=self._render, args=(crops, clips, options), daemon=True, name="vod-render").start()
            return self.snapshot()

    def _render(self, crops, clips, options):
        try:
            destination, names = render_batch(self.source, self.job["title"], self.job["url"], self.media,
                                              crops, clips, options, self.progress)
            with self.lock:
                self.job.update(phase="ready", detail=f"{len(names)} drafts ready for review.",
                                clips=[(destination / name).relative_to(self.clips).as_posix() for name in names])
        except (ValueError, RuntimeError, OSError, ImportError, subprocess.SubprocessError) as error:
            with self.lock:
                self.job.update(phase="crop", detail="The drafts could not be generated.", error=self._error(error))

    @staticmethod
    def _crop(value, name):
        if not isinstance(value, list) or len(value) != 4 or any(type(point) is not int for point in value):
            raise ValueError(f"Draw a {name} crop or enter its X, Y, width, and height.")
        return value

    @staticmethod
    def _error(error):
        if isinstance(error, (ValueError, RuntimeError)):
            return str(error)
        return "Check that the recording is readable and FFmpeg and the project dependencies are installed."
