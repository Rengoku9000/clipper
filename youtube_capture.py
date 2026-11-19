"""Capture a YouTube livestream using yt-dlp and a saved YouTube session."""

import http.cookiejar
import os
import re
import sys
import tempfile
from pathlib import Path

from credential_store import CredentialStore


STORE = CredentialStore(Path(__file__).resolve().parent / ".dashboard" / "youtube-session.dpapi")


def cookies():
    try:
        return STORE.load().get("cookies", "") if os.name == "nt" else ""
    except (OSError, ValueError):
        return ""


def cookie_jar(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > 200000:
        raise ValueError("Import a YouTube cookies.txt export up to 200 KB.")
    if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        raise ValueError("Import YouTube cookies in Netscape cookies.txt format.")
    lines = ["# Netscape HTTP Cookie File"]
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.removeprefix("#HttpOnly_").split("\t")
        if len(parts) != 7:
            raise ValueError("The cookies file contains an invalid cookie row.")
        domain = parts[0].lstrip(".").lower()
        if domain == "youtube.com" or domain.endswith(".youtube.com"):
            lines.append(line)
    if len(lines) == 1:
        raise ValueError("This file has no YouTube cookies. Export cookies from your signed-in YouTube tab.")
    jar = http.cookiejar.MozillaCookieJar()
    # Parse using the standard library, removing the temporary decrypted copy immediately.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "cookies.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            jar.load(path, ignore_discard=True, ignore_expires=False)
        except (OSError, ValueError) as error:
            raise ValueError("The YouTube cookies export could not be read.") from error
    if not list(jar):
        raise ValueError("The YouTube cookies have expired. Export a fresh signed-in session.")
    return jar, "\n".join(lines) + "\n"


def configure(text):
    if os.name != "nt":
        raise ValueError("Encrypted session import requires Windows.")
    _, filtered = cookie_jar(text)
    STORE.save({"cookies": filtered})


class CaptureLogger:
    def debug(self, message):
        pass

    def warning(self, message):
        self.error(message)

    def error(self, message):
        safe = re.sub(r"https?://\S+", "[URL]", str(message))
        print(safe, file=sys.stderr, flush=True)


def main():
    from yt_dlp import YoutubeDL

    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{11}", sys.argv[1]):
        raise SystemExit("Select a valid YouTube livestream.")
    options = {"outtmpl": "-", "format": "b[height<=720]/b", "noplaylist": True,
               "quiet": True, "noprogress": True, "logger": CaptureLogger(),
               "js_runtimes": {"node": {}}, "socket_timeout": 15, "retries": 3,
               "fragment_retries": 3, "hls_use_mpegts": True}
    with YoutubeDL(options) as downloader:
        text = cookies()
        if text:
            jar, _ = cookie_jar(text)
            for cookie in jar:
                downloader.cookiejar.set_cookie(cookie)
        raise SystemExit(downloader.download([f"https://www.youtube.com/watch?v={sys.argv[1]}"]))


if __name__ == "__main__":
    main()
