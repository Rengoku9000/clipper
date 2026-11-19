"""Capture Twitch highlights and export captioned clips for local review."""

import argparse
import csv
import json
import os
import queue
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


DEFAULT_CLIP_PRESET = "highlight"


def youtube_video_id(value):
    url = urlsplit(value)
    path = url.path.split("/")
    video_id = ""
    if url.scheme == "https" and url.netloc.lower() == "youtu.be" and len(path) == 2:
        video_id = path[1]
    if url.scheme == "https" and url.netloc.lower() in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        if url.path == "/watch":
            video_id = parse_qs(url.query).get("v", [""])[0]
        elif len(path) == 3 and path[1] == "live":
            video_id = path[2]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("Enter a YouTube Live video link or a Twitch channel login.")
    return video_id


def youtube_watch_url(video_id):
    return f"https://www.youtube.com/watch?v={video_id}"


CLIP_PRESETS = {
    "quick": ("Quick moment", 16, 9),
    "highlight": ("Gameplay highlight", 28, 17),
    "story": ("Story or reaction", 35, 20),
}


def run(command, directory):
    subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL, check=True)


def report(options, event, **details):
    if options.dashboard_events:
        print("CLIPPER_EVENT " + json.dumps({"event": event, **details}), flush=True)


def srt_time(seconds):
    milliseconds = max(0, round(seconds * 1000))
    minutes, remainder = divmod(milliseconds, 60_000)
    hours, minutes = divmod(minutes, 60)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def normalize_top_caption(value):
    if not isinstance(value, str):
        raise ValueError("Enter text for the top caption.")
    value = " ".join(value.split())
    if len(value) > 56:
        raise ValueError("The top caption can be up to 56 characters.")
    return value


def caption_copy(segments, target, fallback, channel=None, override="", source_url=None):
    candidates = []
    for segment in segments:
        for sentence in re.split(r"(?<=[.!?。！？])\s+", " ".join(segment.text.split())):
            if len(sentence) >= 18 and any(char.isalpha() for char in sentence):
                midpoint = (segment.start + segment.end) / 2
                candidates.append((abs(midpoint - target), sentence))
    quote = min(candidates, key=lambda candidate: candidate[0])[1] if candidates else ""
    short_quote = textwrap.shorten(quote, width=54, placeholder="…")
    title = override or (f"“{short_quote}”" if short_quote and short_quote != "…" else fallback[:56])
    description = f"“{textwrap.shorten(quote, width=360, placeholder='…')}”" if quote else ""
    if channel:
        description = "\n\n".join(filter(None, (description, f"Source: {source_url or f'twitch.tv/{channel}'}")))
    return {"title": title, "description": description}


class Renderer:
    def __init__(self, options):
        self.options = options
        self.model = None

    def prepare_model(self):
        if self.model is None:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(self.options.model, device="cpu", compute_type="int8")

    def render(self, source, start, duration, output, *, concat=False):
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            work = Path(temporary)
            source_args = ["-ss", str(start), "-i", str(source.resolve())]
            if concat:
                source_args = ["-f", "concat", "-safe", "1", "-i", str(source.resolve()),
                               "-ss", str(start)]
            run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", *source_args,
                "-t", str(duration), "-map", "0:v:0",
                "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "cut.mp4",
            ], work)
            layout = (
                "[0:v]split=2[back][front];"
                "[back]scale=360:640:force_original_aspect_ratio=increase:force_divisible_by=2:"
                "reset_sar=1,crop=360:640,gblur=sigma=18,eq=brightness=-0.13,"
                "scale=1080:1920[blur];"
                "[front]scale=1080:1920:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2:reset_sar=1[full];"
                "[blur][full]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30"
            )
            captions = ""
            segments = []
            audio_stream = ""
            if not self.options.no_captions:
                audio_stream = subprocess.check_output([
                    "ffprobe", "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=index", "-of", "csv=p=0", "cut.mp4",
                ], cwd=work, stdin=subprocess.DEVNULL, text=True).strip()
            if audio_stream:
                import numpy as np

                self.prepare_model()
                # Whisper accepts normalized mono audio at 16 kHz.
                samples = subprocess.check_output([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "cut.mp4",
                    "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1",
                ], cwd=work, stdin=subprocess.DEVNULL)
                audio = np.frombuffer(samples, dtype="<f4").copy()
                transcript, _ = self.model.transcribe(audio, vad_filter=True)
                segments = [segment for segment in transcript if segment.text.strip()]
                captions = "\n\n".join(
                    f"{index}\n{srt_time(segment.start)} --> {srt_time(segment.end)}\n"
                    f"{segment.text.strip()}"
                    for index, segment in enumerate(segments, start=1)
                )
            channel = getattr(self.options, "channel", None)
            fallback = channel or source.stem.replace("_", " ").replace("-", " ")
            target = getattr(self.options, "before", duration / 2)
            copy = caption_copy(segments, target, fallback, channel, self.options.top_caption,
                                getattr(self.options, "source_url", None))
            (work / "title.txt").write_text("\n".join(textwrap.wrap(copy["title"], width=22)), encoding="utf-8")
            (work / "caption-copy.json").write_text(json.dumps(copy, ensure_ascii=False), encoding="utf-8")
            title_font = "font=Arial Bold"
            windows_bold = Path(os.environ.get("WINDIR", "")) / "Fonts" / "arialbd.ttf"
            if windows_bold.is_file():
                shutil.copyfile(windows_bold, work / "title-font.ttf")
                title_font = "fontfile=title-font.ttf"
            layout += (
                f",drawtext=textfile=title.txt:expansion=none:{title_font}:fontsize=50:"
                "fontcolor=0xFFD577:borderw=2:bordercolor=0x121018@0.85:"
                "line_spacing=-22:text_align=C:"
                "x=(w-tw)/2:y=h*0.12"
            )
            if captions:
                (work / "captions.srt").write_text(captions + "\n", encoding="utf-8")
                layout += (
                    ",subtitles=captions.srt:"
                    "force_style='FontSize=8,Bold=-1,Outline=1.4,Shadow=0,MarginV=38,MarginL=22,MarginR=22'"
                )
            partial = output.with_name(f".{output.stem}-{uuid4().hex}.mp4")
            try:
                run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "cut.mp4",
                    "-filter_complex", layout, "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000",
                    "-b:a", "128k", "-movflags", "+faststart", str(partial),
                ], work)
                if captions:
                    (work / "captions.srt").replace(output.with_suffix(".srt"))
                else:
                    output.with_suffix(".srt").unlink(missing_ok=True)
                (work / "caption-copy.json").replace(output.with_suffix(".captions.json"))
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
            print(f"Ready for review: {output}", flush=True)


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    end: float


class Buffer:
    def __init__(self, directory):
        self.directory = directory
        self.segments = deque()
        self.anchor = None
        self.rows_read = 0

    def refresh(self):
        manifest = self.directory / "segments.csv"
        if not manifest.exists():
            return
        with manifest.open(newline="", encoding="utf-8") as stream:
            lines = stream.readlines()
        for line in lines[self.rows_read:]:
            if not line.endswith("\n"):
                break
            filename, start, end = next(csv.reader([line]))
            path = self.directory / filename
            segment = Segment(path, float(start), float(end))
            if self.anchor is None:
                self.anchor = manifest.stat().st_mtime - segment.end
            self.segments.append(segment)
            self.rows_read += 1
        while self.segments and self.segments[0].end < self.segments[-1].end - 180:
            self.segments.popleft().path.unlink(missing_ok=True)

    def save(self, renderer, start, end, output, executor):
        selected = [part for part in self.segments if part.end > start and part.start < end]
        if not selected or selected[0].start > start + 0.1 or selected[-1].end < end - 0.1:
            raise RuntimeError("Requested footage is outside the capture buffer")
        # Copy before handing off so buffer cleanup cannot race the render.
        temporary = tempfile.TemporaryDirectory(dir=self.directory)
        work = Path(temporary.name)
        try:
            for part in selected:
                shutil.copyfile(part.path, work / part.path.name)
            (work / "join.txt").write_text(
                "".join(f"file '{part.path.name}'\n" for part in selected), encoding="ascii"
            )
            return executor.submit(self.render_saved, temporary, renderer, start - selected[0].start,
                                   end - start, output)
        except BaseException:
            temporary.cleanup()
            raise

    @staticmethod
    def render_saved(temporary, renderer, start, duration, output):
        with temporary as directory:
            renderer.render(Path(directory) / "join.txt", max(0, start), duration, output, concat=True)


class ChatDetector:
    def __init__(self):
        self.history = deque(maxlen=30)
        self.users = set()
        self.messages = 0
        self.last_count = 0
        self.last_messages = 0

    def finish_window(self):
        count = len(self.users)
        self.users.clear()
        self.last_count, self.last_messages = count, self.messages
        self.messages = 0
        spike = len(self.history) >= 6 and count >= max(5, 3 * statistics.median(self.history))
        self.history.append(count)
        return spike


def twitch_chat(channel, events, stop):
    from websockets.sync.client import connect

    token = os.environ["TWITCH_USER_TOKEN"]
    headers = {"Authorization": f"Bearer {token}", "Client-Id": os.environ["TWITCH_CLIENT_ID"]}

    def request(path, body=None):
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            f"https://api.twitch.tv/helix/{path}", data,
            {**headers, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            return json.load(response)

    try:
        broadcaster = request(f"users?login={channel}")["data"][0]["id"]
        reader = request("users")["data"][0]["id"]
    except Exception as error:
        print(f"Twitch chat setup failed: {error}. Manual capture is still available.", file=sys.stderr)
        return

    url = "wss://eventsub.wss.twitch.tv/ws"
    while not stop.is_set():
        try:
            with connect(url, open_timeout=8) as socket:
                welcome = json.loads(socket.recv(timeout=10))
                session = welcome["payload"]["session"]
                request("eventsub/subscriptions", {
                    "type": "channel.chat.message",
                    "version": "1",
                    "condition": {"broadcaster_user_id": broadcaster, "user_id": reader},
                    "transport": {"method": "websocket", "session_id": session["id"]},
                })
                events.put(("chat_reset", None))
                print(f"Reading Twitch chat for {channel}.", flush=True)
                while not stop.is_set():
                    notice = json.loads(socket.recv(timeout=session["keepalive_timeout_seconds"] + 5))
                    kind = notice["metadata"]["message_type"]
                    if kind == "notification":
                        message = notice["payload"]["event"]
                        if not any(badge["set_id"] == "bot" for badge in message["badges"]):
                            events.put(("chat", message["chatter_user_id"]))
                    elif kind in {"session_reconnect", "revocation"}:
                        break
        except Exception as error:
            print(f"Twitch chat disconnected: {error}", file=sys.stderr)
        events.put(("chat_reset", None))
        stop.wait(5)


def manual_marks(events):
    for line in sys.stdin:
        if line.strip().lower() == "q":
            events.put(("quit", None))
            return
        events.put(("manual", time.time()))


def stop_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def capture(options, renderer):
    events = queue.Queue()
    stop = threading.Event()
    youtube = options.source_url.startswith("https://www.youtube.com/")
    if options.chat and not youtube:
        if not all(os.environ.get(key) for key in ("TWITCH_USER_TOKEN", "TWITCH_CLIENT_ID")):
            raise ValueError("Set TWITCH_USER_TOKEN and TWITCH_CLIENT_ID to enable Twitch chat")
    # NumPy's Windows initialization can wait on stdin while the input thread reads it.
    if not options.no_captions:
        renderer.prepare_model()
    threading.Thread(target=manual_marks, args=(events,), daemon=True).start()
    if options.chat:
        from youtube_chat import read_chat
        reader = read_chat if youtube else twitch_chat
        threading.Thread(target=reader, args=(options.channel, events, stop), daemon=True).start()

    options.output_dir.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=options.output_dir.resolve()) as temporary,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        work = Path(temporary)
        buffer = Buffer(work)
        stream_command = [
            sys.executable, "-m", "streamlink", "--stdout",
            options.source_url, options.quality,
        ]
        if youtube:
            stream_command = [sys.executable, "-u", str(Path(__file__).with_name("youtube_capture.py")), options.channel]
        if options.input:
            stream_command = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1",
                "-i", str(options.input.resolve()), "-c", "copy", "-f", "mpegts", "pipe:1",
            ]
        stream = subprocess.Popen(
            stream_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        )
        try:
            ffmpeg = subprocess.Popen([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
                "-segment_time", "2", "-segment_list", "segments.csv",
                "-segment_list_type", "csv", "part-%06d.ts",
            ], cwd=work, stdin=stream.stdout)
            stream.stdout.close()
            try:
                capture_loop(options, renderer, ffmpeg, buffer, events, executor)
            finally:
                stop_process(ffmpeg)
        finally:
            stop.set()
            stream.stdout.close()
            stop_process(stream)


def collect_renders(options, jobs, *, wait=False):
    for job in list(jobs):
        if not wait and not job.done():
            continue
        output, marked_at = jobs.pop(job)
        try:
            job.result()
        except Exception as error:
            print(f"Clip render failed: {error}", file=sys.stderr)
            report(options, "failed", message=str(error))
        else:
            report(options, "ready", path=str(output.resolve()),
                   latency=round(time.monotonic() - marked_at, 1))


def capture_loop(options, renderer, ffmpeg, buffer, events, executor):
    print("Capturing. Press Enter to mark a highlight. Type q and press Enter to stop.", flush=True)
    detector = ChatDetector()
    next_window = time.monotonic() + 10
    last_auto_mark = float("-inf")
    pending = deque()
    jobs = {}
    last_end = None
    while True:
        buffer.refresh()
        if buffer.segments and buffer.segments[-1].end != last_end:
            last_end = buffer.segments[-1].end
            report(options, "buffer", seconds=round(last_end - buffer.segments[0].start, 1),
                   captured=round(last_end, 1), ready=last_end >= options.before)
        collect_renders(options, jobs)

        try:
            kind, value = events.get(timeout=0.25)
        except queue.Empty:
            kind, value = None, None
        if kind == "quit":
            collect_renders(options, jobs, wait=True)
            return
        if kind == "chat_reset":
            detector = ChatDetector()
            next_window = time.monotonic() + 10
        elif kind == "chat":
            detector.users.add(value)
            detector.messages += 1
        if time.monotonic() >= next_window:
            spike = detector.finish_window()
            if options.chat:
                report(options, "chat", messages=detector.last_messages,
                       users=detector.last_count, spike=spike)
            if spike and time.monotonic() - last_auto_mark >= max(30, options.before + options.after):
                last_auto_mark = time.monotonic()
                if kind != "manual":
                    kind, value = "spike", time.time() - options.chat_offset
            next_window += 10
        if kind in {"manual", "spike"}:
            if not buffer.segments or buffer.segments[-1].end < options.before:
                print("Waiting for enough footage. Mark again after the buffer fills.", flush=True)
            elif len(pending) + len(jobs) >= 5:
                print("Render queue is full. Skipping this highlight.", file=sys.stderr)
            else:
                center = max(options.before, value - buffer.anchor)
                pending.append((center - options.before, center + options.after, time.monotonic()))
                print(f"Highlight marked ({kind}). Waiting for post-roll.", flush=True)
                report(options, "marked", trigger=kind)

        for start, end, marked_at in list(pending):
            if buffer.segments and buffer.segments[-1].end >= end:
                pending.remove((start, end, marked_at))
                stamp = time.strftime("%Y%m%d-%H%M%S")
                output = options.output_dir / f"{options.channel}-{stamp}-{uuid4().hex[:8]}.mp4"
                try:
                    job = buffer.save(renderer, start, end, output, executor)
                    jobs[job] = output, marked_at
                    report(options, "rendering")
                except RuntimeError as error:
                    print(error, file=sys.stderr)
                    report(options, "failed", stage="waiting", message=str(error))
        if ffmpeg.poll() is not None:
            collect_renders(options, jobs, wait=True)
            raise RuntimeError(f"Capture ended (FFmpeg exit {ffmpeg.returncode}). Restart to reconnect.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="Render a clip from a local recording")
    render.add_argument("source", type=Path)
    render.add_argument("output", type=Path)
    render.add_argument("--start", type=float, default=0)
    _, default_before, default_after = CLIP_PRESETS[DEFAULT_CLIP_PRESET]
    render.add_argument("--duration", type=float, default=default_before + default_after)

    live = commands.add_parser("live", help="Capture a Twitch channel or YouTube Live video")
    live.add_argument("channel")
    live.add_argument("--input", type=Path, help="Loop a local recording in real time for rehearsal")
    live.add_argument("--quality", default="best")
    live.add_argument("--chat", action="store_true")
    live.add_argument("--dashboard-events", action="store_true", help=argparse.SUPPRESS)
    live.add_argument("--chat-offset", type=float, default=5)
    live.add_argument("--before", type=float, default=default_before)
    live.add_argument("--after", type=float, default=default_after)
    live.add_argument("--output-dir", type=Path, default=Path("clips"))

    for command in (render, live):
        command.add_argument("--model", default="base")
        command.add_argument("--no-captions", action="store_true")
        command.add_argument("--top-caption", default="", help="Headline shown at the top of each clip")
    options = parser.parse_args()
    renderer = Renderer(options)
    try:
        options.top_caption = normalize_top_caption(options.top_caption)
        if options.command == "render":
            if options.start < 0 or options.duration <= 0:
                parser.error("Start must be nonnegative and duration must be positive")
            if options.source.resolve() == options.output.resolve():
                parser.error("Use a different file for the output")
            renderer.render(options.source, options.start, options.duration, options.output)
        else:
            if re.fullmatch(r"[a-zA-Z0-9_]{1,25}", options.channel):
                options.source_url = f"https://www.twitch.tv/{options.channel}"
            else:
                options.channel = youtube_video_id(options.channel)
                options.source_url = youtube_watch_url(options.channel)
                if options.input:
                    parser.error("Local rehearsal is available for Twitch captures")
            if options.before <= 0 or options.after <= 0 or options.before + options.after > 120:
                parser.error("Pre-roll and post-roll must be positive and total at most 120 seconds")
            capture(options, renderer)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
