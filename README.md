# Live stream clipper

This first version captures one Twitch channel per process and writes vertical clips for local review. [PARKERRYT](https://b4e0vdqv6zgqeqj4pfgm.apps.whop.com/c/campaigns/e41de449-3eca-4343-a11c-b194e7a3379a) is the candidate for a live rehearsal once the campaign brief confirms eligible footage and editing requirements.

## Setup on Windows

Install Python 3.12 and put FFmpeg and FFprobe on PATH. The FFmpeg build must include libx264 and the subtitles and drawtext filters. Run from this folder in PowerShell.

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Captioning runs locally with faster-whisper on CPU. The first captioned render downloads the selected model. Use `--model small` for a larger model if needed, or `--no-captions` to check the video workflow without downloading a model.

If `.venv-cache/whisper-base/model.bin` is present, pass `--model .venv-cache/whisper-base` to reuse the rehearsal model without a network request.

## Rehearse with footage you can use

```powershell
& .\.venv\Scripts\python.exe clipper.py render recording.mp4 clips\preview.mp4 --start 60 --duration 30
& .\.venv\Scripts\python.exe clipper.py live rehearsal --input recording.mp4 --before 5 --after 3
```

For the second command, wait for the buffer to fill, then press Enter to mark a highlight. The MP4 appears in `clips` after post-roll arrives and rendering completes. The whole original frame appears over a 1080 × 1920 blurred background made from the same footage. A top caption defaults to the Twitch channel or recording name. Set it with `--top-caption "Your caption"` or the dashboard’s **Start capture** form. When speech is detected, compact subtitles appear near the bottom and are also written as an SRT.

## Try an approved Twitch stream

```powershell
& .\.venv\Scripts\python.exe clipper.py live APPROVED_TWITCH_LOGIN
```

Press Enter for a manual clip. The default gameplay highlight is 45 seconds, with 28 seconds before the mark and 17 seconds after. **Start capture** also offers Quick moment (25 seconds, 16 before and 9 after) and Story or reaction (55 seconds, 35 before and 20 after). For command-line captures, use `--before` and `--after` to set the window. Local recording renders default to 45 seconds and accept `--duration`.

For automatic chat spikes, set `TWITCH_CLIENT_ID` and `TWITCH_USER_TOKEN` in the process environment and add `--chat`. Use a Twitch user token issued to that client with `user:read:chat` scope. Tune `--chat-offset` after comparing a few clips with the stream timeline. Increase it to move cuts earlier on the capture timeline.

The chat detector needs at least one minute of baseline. A spike requires at least five unique chatters and at least three times the recent median count. The cooldown is at least 30 seconds and grows with longer clip windows to avoid overlapping chat-triggered clips. If the video feed ends, restart capture to reconnect. Press Ctrl+C to stop.

## Dashboard

Start the dashboard from the project folder and open [localhost:8765](http://localhost:8765).

```powershell
& .\.venv\Scripts\python.exe dashboard.py
```

To make the rehearsal checkbox available, add `--rehearsal-input recording.mp4`. The dashboard indexes existing MP4s in `clips`. Capture events and publishing history are saved to SQLite in `.dashboard`. Detailed workflow history starts with captures you run from the dashboard. Chat statistics require the Twitch credentials described above.

### Connect publishing accounts

Set app credentials in the PowerShell session before starting the dashboard. Then use **Accounts** to sign in and choose the destination from each clip's preview. OAuth tokens stay in dashboard process memory. Restarting requires reconnecting. Uploads are started manually from the clip preview.

- **YouTube** requires YouTube Data API v3 and a Google Cloud Desktop OAuth client. Set `CLIPPER_GOOGLE_CLIENT_ID` and `CLIPPER_GOOGLE_CLIENT_SECRET`. The dashboard uses `http://localhost:8765/oauth/youtube` as the default loopback callback. Add your Google account as a test user if the consent screen is in testing mode. The upload dialog defaults to private.
- **Instagram and Facebook** use Facebook Login for Business. In **Accounts → Set up Meta**, enter the App ID, Configuration ID and App Secret. They are loaded for the current dashboard session. For launch-time setup, set `CLIPPER_META_CLIENT_ID`, `CLIPPER_META_CLIENT_SECRET`, and `CLIPPER_META_CONFIG_ID` in the launching environment instead. Configure `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, and `instagram_content_publish`. Use a Page where you have permission to create content. Instagram needs a professional account linked to that Page. The default callback is `http://localhost:8765/oauth/meta`. If the app requires HTTPS, set `CLIPPER_META_REDIRECT_URI` to an approved HTTPS callback that forwards only `/oauth/meta` to the dashboard. Meta may require app roles or approved access for the accounts you connect.

Use the Publishing tab to review uploads and platform responses. An interrupted or ambiguous submission requires checking the platform before another upload. Campaign submissions, automatic streamer discovery, cross-stream ranking, and post engagement analytics are outside this dashboard's current scope.

If Meta completes sign-in but the dashboard cannot discover your Page, set `CLIPPER_META_PAGE_ID` in the launching environment to the Facebook Page ID you selected during consent. Clipper Studio then requests that Page and its linked Instagram account directly from Meta.
