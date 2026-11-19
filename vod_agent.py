"""Find and render vertical highlight drafts from an ended YouTube livestream."""

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from clipper import Renderer, caption_copy, run, srt_time, youtube_video_id, youtube_watch_url
from youtube_capture import CaptureLogger, load_session


ROOT = Path(__file__).resolve().parent
SAMPLE_RATE = 16000
ANALYSIS_CHUNK = 600
REACTION = re.compile(r"\b(no way|oh my god|let'?s go|what just happened|clutch|we won|we got it)\b", re.I)
MOMENT_TAGS = (
    (re.compile(r"\b(clutch|last one alive|one hp)\b", re.I), "#ClutchPlay"),
    (re.compile(r"\b(comeback|came back from)\b", re.I), "#GamingComeback"),
    (re.compile(r"\b(boss fight|final boss|beat the boss)\b", re.I), "#BossFight"),
    (re.compile(r"\b(speedrun|personal best|world record)\b", re.I), "#Speedrun"),
)
CAPTION_STYLE = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Speech,Arial,52,&H00FFFFFF,&H00FFFFFF,&H00141414,&H00000000,-1,0,0,0,100,100,0,0,1,5,0,8,80,80,628,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
ASS_TEXT = str.maketrans({"{": "(", "}": ")", "\\": "＼"})


@dataclass(frozen=True)
class Highlight:
    start: float
    end: float
    anchor: float
    reason: str


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def download_replay(url, work, *, allow_upload=False, progress=None):
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    options = {
        "outtmpl": str(work / "replay.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mkv",
        "noplaylist": True, "quiet": True, "noprogress": True,
        "logger": CaptureLogger(), "js_runtimes": {"node": {}},
        "socket_timeout": 15, "retries": 3, "fragment_retries": 3,
        "http_chunk_size": 4 * 1024 * 1024,
    }
    if progress:
        def on_download(info):
            total = info.get("total_bytes") or info.get("total_bytes_estimate")
            if info.get("status") == "downloading" and total:
                percent = min(100, int(100 * info.get("downloaded_bytes", 0) / total))
                progress("downloading", f"Downloading media track · {percent // 5 * 5}%")
        options["progress_hooks"] = [on_download]
    try:
        with YoutubeDL(options) as downloader:
            load_session(downloader)
            info = downloader.extract_info(url, download=False)
            status = info.get("live_status")
            if status == "post_live":
                raise ValueError("The stream has ended and YouTube is still preparing the replay.")
            if status == "not_live" and not allow_upload:
                raise ValueError("This is a regular YouTube upload. Add --allow-upload to analyze it.")
            if status != "was_live" and not (allow_upload and status == "not_live"):
                raise ValueError("Enter a YouTube livestream link with an available replay.")
            print("Downloading the replay…", flush=True)
            downloader.process_info(info)
    except DownloadError as error:
        raise RuntimeError("The replay could not be downloaded. Check playback and the saved YouTube session.") from error
    files = [item for item in work.glob("replay.*") if item.suffix in {".mp4", ".mkv", ".webm"}]
    if len(files) != 1:
        raise RuntimeError("Expected one downloaded replay with merged video and audio.")
    return files[0], info.get("title") or "YouTube highlight"


def probe(source):
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
        "-of", "json", str(source),
    ], stdin=subprocess.DEVNULL)
    media = json.loads(result)
    video = next((stream for stream in media.get("streams", []) if stream["codec_type"] == "video"), None)
    duration = float(media.get("format", {}).get("duration", 0))
    if video is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Choose a recording with a video track and a known duration.")
    has_audio = any(stream["codec_type"] == "audio" for stream in media["streams"])
    return video["width"], video["height"], duration, has_audio


def validate_crop(values, width, height, name):
    x, y, crop_width, crop_height = values
    if min(x, y) < 0 or min(crop_width, crop_height) < 2 or x + crop_width > width or y + crop_height > height:
        raise ValueError(f"The {name} crop must fit within the {width}×{height} source.")
    return f"crop={crop_width}:{crop_height}:{x}:{y}:exact=1"


def analyze(source, duration, has_audio, options, clips, progress=None):
    if not has_audio:
        return [], []

    import numpy as np

    renderer = Renderer(options)
    renderer.prepare_model()
    words, levels = [], []
    if clips:
        ranges = []
        for clip in sorted(clips, key=lambda clip: clip.start):
            if ranges and clip.start <= ranges[-1][1]:
                left, right = ranges[-1]
                ranges[-1] = (left, max(right, clip.end))
            else:
                ranges.append((clip.start, clip.end))
    else:
        ranges = [(start, min(duration, start + ANALYSIS_CHUNK))
                  for start in range(0, math.ceil(duration), ANALYSIS_CHUNK)]
    for start, end in ranges:
        message = f"Analyzing {start:.1f}–{end:.1f}s of {duration:.1f}s…"
        if progress:
            progress("analyzing", message)
        else:
            print(message, flush=True)
        raw = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", str(source),
            "-t", str(end - start), "-map", "0:a:0", "-af", "aresample=16000:async=1:first_pts=0",
            "-ac", "1", "-f", "f32le", "pipe:1",
        ], stdin=subprocess.DEVNULL)
        samples = np.frombuffer(raw, dtype="<f4")
        if not samples.size:
            continue
        for offset in range(0, samples.size, SAMPLE_RATE):
            second = samples[offset:offset + SAMPLE_RATE]
            levels.append((start + offset // SAMPLE_RATE, float(np.sqrt(np.mean(second ** 2)))))
        transcript, _ = renderer.model.transcribe(samples, vad_filter=True, word_timestamps=True)
        for segment in transcript:
            for word in segment.words or []:
                if word.word.strip() and word.end > word.start:
                    words.append((start + word.start, min(end, start + word.end), word.word))
    return sorted(words), levels


def select_highlights(words, levels, duration, count, clip_length):
    if not levels:
        return []
    import numpy as np

    amplitudes = [level for _, level in levels]
    ceiling = max(float(np.percentile(amplitudes, 95)), max(amplitudes) * 0.3, 0.002)
    if max(amplitudes) < 0.002 and not words:
        return []
    candidates = []
    for (second, level), (_, previous) in zip(levels, [(0, 0), *levels]):
        if level >= max(0.002, ceiling * 0.6) and level > previous:
            score = min(2, level / ceiling) + min(1, (level - previous) / ceiling)
            candidates.append((score, min(duration, second + 0.5), "Audio reaction or action peak"))
    for cue in subtitle_cues(words, 0, duration):
        if REACTION.search(cue.text):
            candidates.append((4, cue.start, "Spoken reaction"))
    if not candidates and words:
        candidates.append((1, words[len(words) // 2][0], "Spoken moment"))

    selected = []
    word_starts = [left for left, _, _ in words]
    for _, anchor, reason in sorted(candidates, key=lambda match: (-match[0], match[1])):
        length = min(duration, clip_length)
        start = max(0, min(duration - length, anchor - length * 0.55))
        end = start + length
        # Move nearby boundaries outward to keep the words at the cut intact.
        start = keep_word_intact(words, word_starts, start, 0)
        end = keep_word_intact(words, word_starts, end, 1)
        if all(end <= clip.start or start >= clip.end for clip in selected):
            selected.append(Highlight(start, end, anchor, reason))
        if len(selected) == count:
            break
    return selected


def keep_word_intact(words, starts, boundary, edge):
    index = bisect_left(starts, boundary) - 1
    if index >= 0:
        word = words[index]
        if word[0] < boundary < word[1] and abs(word[edge] - boundary) < 1:
            return word[edge]
    return boundary


def subtitle_cues(words, start, end):
    cues, line = [], ""
    for left, right, word in words:
        if right <= max(left, start):
            continue
        if left >= end:
            break
        word = word.strip()
        combined = f"{line} {word}".strip()
        if line and (len(combined) > 36 or right - cue_start > 2.8 or left - cue_end > 0.6):
            cues.append(Cue(cue_start - start, cue_end - start, line))
            combined = word
            line = ""
        if not line:
            cue_start = max(left, start)
        line = combined
        cue_end = min(right, end)
    if line:
        cues.append(Cue(cue_start - start, cue_end - start, line))
    return cues


def post_copy(cues, clip, title, game, source_url):
    sentences = []
    for cue in cues:
        if sentences and cue.start - sentences[-1].end <= 0.7 and not sentences[-1].text.endswith((".", "?", "!")):
            previous = sentences[-1]
            sentences[-1] = Cue(previous.start, cue.end, f"{previous.text} {cue.text}")
        else:
            sentences.append(cue)
    copy = caption_copy(sentences, clip.anchor - clip.start, title)
    tags = ["#Gameplay", "#StreamHighlights", "#GamingClips"]
    game_tag = "".join(re.findall(r"[^\W_]+", game))
    if game_tag:
        tags.insert(0, f"#{game_tag}")
    transcript = " ".join(cue.text for cue in cues)
    moment_tag = next((tag for pattern, tag in MOMENT_TAGS if pattern.search(transcript)), "")
    if moment_tag:
        tags.append(moment_tag)
    elif clip.reason == "Spoken reaction":
        tags.append("#GamingReaction")
    tags = list(dict.fromkeys(tags))
    if not copy["description"]:
        copy["description"] = f"Gameplay highlight from {title}."
    link = f"{source_url}&t={int(clip.start)}s" if source_url else ""
    copy["description"] = "\n\n".join(filter(None, (
        copy["description"], " ".join(tags), f"Source: {link}" if link else "",
    )))
    return {**copy, "hashtags": tags, "source_kind": "vod", "source_url": source_url,
            "start": clip.start, "end": clip.end, "reason": clip.reason}


def write_subtitles(output, cues):
    srt = "\n\n".join(
        f"{index}\n{srt_time(cue.start)} --> {srt_time(cue.end)}\n{cue.text}"
        for index, cue in enumerate(cues, 1)
    )
    output.with_suffix(".srt").write_text(srt + "\n", encoding="utf-8")
    ass = output.with_suffix(".ass")
    lines = [CAPTION_STYLE]
    for cue in cues:
        start = srt_time(cue.start).replace(",", ".")[:-1]
        end = srt_time(cue.end).replace(",", ".")[:-1]
        lines.append(f"Dialogue: 0,{start},{end},Speech,,0,0,0,,{cue.text.translate(ASS_TEXT)}\n")
    ass.write_text("".join(lines), encoding="utf-8")
    return ass


def render_draft(source, clip, words, crops, output, title, game, url):
    cues = subtitle_cues(words, clip.start, clip.end)
    layout = (
        f"[0:v]split=2[cam][game];[cam]{crops[0]},"
        "scale=1080:600:force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1,"
        f"pad=1080:600:(ow-iw)/2:(oh-ih)/2[cam_panel];[game]{crops[1]},split=2[back][front];"
        "[back]scale=270:330:force_original_aspect_ratio=increase:force_divisible_by=2:reset_sar=1,"
        "crop=270:330,gblur=sigma=12,eq=brightness=-0.22,scale=1080:1320[blur];"
        "[front]scale=1080:1320:force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1[play];"
        "[blur][play]overlay=(W-w)/2:(H-h)/2[game_panel];"
        "[cam_panel][game_panel]vstack,setsar=1,fps=30"
    )
    ass = write_subtitles(output, cues) if cues else None
    if ass:
        layout += f",ass={ass.name}"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(clip.start), "-i", str(source),
        "-t", str(clip.end - clip.start), "-filter_complex_threads", "2",
        "-filter_complex", layout + "[out]", "-map", "[out]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", output.name,
    ], output.parent)
    if ass:
        ass.unlink()
    copy = post_copy(cues, clip, title, game, url)
    output.with_suffix(".captions.json").write_text(json.dumps(copy, ensure_ascii=False, indent=2), encoding="utf-8")


def create_drafts(options):
    url = ""
    if options.source.startswith(("http://", "https://")):
        url = youtube_watch_url(youtube_video_id(options.source))
    if options.recording and not url:
        raise ValueError("Use --recording with the YouTube URL for that recording.")
    if not url or options.recording:
        source = (options.recording or Path(options.source)).resolve(strict=True)
        if not source.is_file():
            raise ValueError("Choose a local recording file or a YouTube livestream URL.")
        title = source.stem.replace("_", " ").replace("-", " ")

    parent = options.output_dir.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vod-", dir=parent) as temporary:
        work = Path(temporary)
        if url and not options.recording:
            source, title = download_replay(url, work, allow_upload=options.allow_upload)
        media = probe(source)
        crops, clips = prepare_edits(media, options.facecam, options.gameplay, options.ranges)
        destination, _ = render_batch(source, title, url, media, crops, clips, options)
    print(f"Drafts ready for review: {destination}", flush=True)


def prepare_edits(media, facecam, gameplay, ranges):
    width, height, duration, _ = media
    crops = (
        validate_crop(facecam, width, height, "facecam"),
        validate_crop(gameplay or (0, 0, width, height), width, height, "gameplay"),
    )
    clips = []
    for start, end in ranges or []:
        if not 0 <= start < end <= duration or end - start > 120:
            raise ValueError(f"Set a range within the {duration:.2f}s replay and no longer than 120 seconds.")
        clips.append(Highlight(start, end, (start + end) / 2, "Selected time range"))
    return crops, clips


def render_batch(source, title, url, media, crops, clips, options, progress=None):
    _, _, duration, has_audio = media
    if progress:
        progress("analyzing", "Preparing audio for highlight analysis…")
    words, levels = analyze(source, duration, has_audio, options, clips, progress)
    if not clips:
        clips = select_highlights(words, levels, duration, options.count, options.duration)
    if not clips:
        raise ValueError("No highlights found from the audio. Enter a time range to render a gameplay moment.")
    parent = options.output_dir.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".vod-", dir=parent) as temporary:
        staging = Path(temporary) / "drafts"
        staging.mkdir()
        names = []
        for index, clip in enumerate(clips, 1):
            message = f"Rendering {index}/{len(clips)} · {clip.start:.1f}–{clip.end:.1f}s · {clip.reason}"
            if progress:
                progress("rendering", message)
            else:
                print(message, flush=True)
            name = f"highlight-{index:02}.mp4"
            render_draft(source, clip, words, crops, staging / name, title, options.game, url)
            names.append(name)
        destination = parent / f"vod-{uuid4().hex[:10]}"
        staging.rename(destination)
    return destination, names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="YouTube replay URL or path to a local recording")
    parser.add_argument("--allow-upload", action="store_true", help="Also accept regular YouTube uploads")
    parser.add_argument("--recording", type=Path, help="Use an existing copy of the linked YouTube video")
    parser.add_argument("--facecam", nargs=4, type=int, metavar=("X", "Y", "W", "H"), required=True)
    parser.add_argument("--gameplay", nargs=4, type=int, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--game", default="", help="Game name for the post hashtags")
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--duration", type=int, default=30, help="Target length of each automatic pick in seconds")
    parser.add_argument("--range", dest="ranges", action="append", nargs=2, type=float, metavar=("START", "END"),
                        help="Pick a time range in seconds. Repeat to render several manual picks.")
    model = ROOT / ".venv-cache" / "whisper-base"
    parser.add_argument("--model", default=str(model) if (model / "model.bin").is_file() else "base")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "clips" / "vod-drafts")
    options = parser.parse_args()
    if not 1 <= options.count <= 10 or not 10 <= options.duration <= 60:
        parser.error("Choose 1–10 automatic clips and a target duration of 10–60 seconds.")
    try:
        create_drafts(options)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
