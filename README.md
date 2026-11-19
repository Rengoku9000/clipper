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

For the second command, wait for the buffer to fill, then press Enter to mark a highlight. The MP4 appears in `clips` after post-roll arrives and rendering completes. The whole original frame appears over a 1080 × 1920 blurred background made from the same footage. The top caption uses a short quote from the speech near the highlight and falls back to the Twitch channel or recording name. Override it with `--top-caption "Your caption"` or the dashboard’s **Start capture** form. When speech is detected, compact subtitles appear near the bottom and are also written as an SRT. The Publish dialog prefills an editable title and post caption from the saved copy.

## Try an approved Twitch stream

```powershell
& .\.venv\Scripts\python.exe clipper.py live APPROVED_TWITCH_LOGIN
```

Press Enter for a manual clip. The default gameplay highlight is 45 seconds, with 28 seconds before the mark and 17 seconds after. **Start capture** also offers Quick moment (25 seconds, 16 before and 9 after) and Story or reaction (55 seconds, 35 before and 20 after). For command-line captures, use `--before` and `--after` to set the window. Local recording renders default to 45 seconds and accept `--duration`.

For automatic chat spikes, set `TWITCH_CLIENT_ID` and `TWITCH_USER_TOKEN` in the process environment and add `--chat`. Use a Twitch user token issued to that client with `user:read:chat` scope. Tune `--chat-offset` after comparing a few clips with the stream timeline. Increase it to move cuts earlier on the capture timeline.

For the Windows dashboard, register a Public app in the Twitch Developer Console and run `& .\.venv\Scripts\python.exe twitch_chat_auth.py YOUR_CLIENT_ID`. Follow the Twitch activation prompt. The dashboard uses the Windows-encrypted authorization to enable **Detect chat spikes** and renews expiring access when a chat-enabled capture starts.

YouTube Live also supports **Detect chat spikes**. Enable YouTube Data API v3 in your Google Cloud project, create an API key restricted to that API, and paste it under **Accounts → Set up YouTube chat**. The Windows dashboard encrypts the key locally. Select **YouTube Live** and enable detection when starting a capture. The reader counts new chat messages, skips the initial chat history, respects YouTube’s polling interval, and stops on quota exhaustion or unavailable chat. API quota is shared across captures in the Google project. For the CLI, set `YOUTUBE_CHAT_API_KEY` and run `clipper.py live YOUTUBE_VIDEO_URL --chat`.

YouTube video capture uses yt-dlp with FFmpeg and a Node.js runtime for YouTube player support. If YouTube requires sign-in, export youtube.com cookies from the browser where playback works in Netscape cookies.txt format and import the file under **Accounts → YouTube video capture → Import YouTube session**. The Windows dashboard encrypts the saved session and loads it into the YouTube worker’s cookie jar. Renew the export if YouTube invalidates the session. Follow the [yt-dlp cookie export instructions](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).

The chat detector needs at least one minute of baseline. A spike requires at least five unique chatters and at least three times the recent median count. The cooldown is at least 30 seconds and grows with longer clip windows to avoid overlapping chat-triggered clips. If the video feed ends, restart capture to reconnect. Press Ctrl+C to stop.

## Clip a past YouTube stream

In the dashboard, open **Clip library → Clip past video**. Paste a YouTube link or enter the path to a local recording. Preview a frame where both the facecam and gameplay are visible, then draw each crop or enter its pixel coordinates. Set the game name for hashtags and generate drafts. To pick your own moments, enter start–end ranges in seconds. The crop settings apply to the full batch, so choose ranges with a consistent layout. The agent runs in the background while the dashboard server is open. Open its job card to check progress or preview the results.

Run the VOD agent from the project folder with an ended YouTube livestream URL or a local recording. The facecam and optional gameplay crops use source pixels in `X Y WIDTH HEIGHT` order. The example coordinates are for a 1920 × 1080 source with a camera in the upper left. Adjust them to match your recording.

```powershell
& .\.venv\Scripts\python.exe vod_agent.py 'https://www.youtube.com/watch?v=YOUR_VIDEO_ID' `
    --facecam 32 24 480 270 --game 'Minecraft'
```

Use `--allow-upload` for a regular YouTube video. If you already have a local copy of the video, supply its YouTube URL and `--recording PATH` to use the local file and keep the timestamped source link in the post copy.

The agent ranks audio peaks and spoken reactions, then renders up to three non-overlapping clips targeting 30 seconds each. Use `--count` and `--duration` to adjust the batch, or repeat `--range START END` to select specific times in seconds. By default the gameplay panel uses the full source frame. Supply `--gameplay X Y WIDTH HEIGHT` if you want to exclude the original facecam or another overlay.

Each draft has the facecam above the gameplay, burned-in subtitles when speech is detected, an editable SRT, and suggested post copy with hashtags. The dashboard's **Clip library** picks up the default output folder and prefills the Publish dialog. To change burned-in captions, the video needs to be rendered again. VOD drafts are published manually. Transcription runs locally with Whisper.

## Dashboard

Start the dashboard from the project folder and open [localhost:8765](http://localhost:8765).

```powershell
& .\.venv\Scripts\python.exe dashboard.py
```

To make the rehearsal checkbox available, add `--rehearsal-input recording.mp4`. The dashboard indexes existing MP4s in `clips`. Capture events and publishing history are saved to SQLite in `.dashboard`. Detailed workflow history starts with captures you run from the dashboard. Chat statistics require the Twitch credentials described above.

The dashboard runs separate **Twitch Clipping** and **YouTube Clipping** agents. To clip a public YouTube Live video, open **Start capture**, choose **YouTube Live**, and paste its video link. Once the buffer fills, use **Mark highlight** to export a captioned vertical clip to the shared library. Twitch supports optional chat-spike detection. The stream monitors under **Workflow** show current counts and recent history for each platform using the local SQLite capture log. Local rehearsals are excluded.

The **Publishing Agent** queues each new live clip automatically for YouTube Shorts, Instagram Reels, and Facebook Reels as soon as rendering finishes. Automatic posts use the generated title and description. YouTube visibility is public and the gameplay clips are marked as not made for kids. Keep one destination connected per platform. If it’s missing or ambiguous, that platform waits while the other destinations can publish. Existing clips and local rehearsals stay available for manual review. Queued automatic posts resume after restart, and interrupted uploads are flagged for verification.

Under **Publishing → Automatic publishing**, toggle YouTube, Instagram, and Facebook independently. Choices save immediately and survive restarts. Turning a platform off pauses pending automatic uploads and excludes it from new clips. Turning it back on resumes the pending uploads. Uploads already in progress finish normally. Manual publishing remains available from each clip’s preview.

The **Filtering Agent** runs before every upload, including manual submissions and the YouTube media handoff. It uses NudeNet locally to inspect every decoded frame for exposed intimate body parts, checks the central stream area at a larger scale, and checks captions and subtitles for sexual-content language. Suspected content and incomplete or failed screening are held for review. **Publishing → Content screening** lists results and timestamps with Preview buttons. Results are cached against the clip and subtitle contents and are checked again if those contents change. Model initialization and screening run on the CPU and add time before publication. This screening reduces risk but does not guarantee platform compliance or detect every form of sexual activity, especially scenes without detectable nudity or matching text. It does not change existing account restrictions or monetization eligibility.

For a visual false positive, open **Approve after review** in Content screening, watch the video, and confirm the flagged scene is suitable. Approval applies to that exact clip and its subtitles. It resumes held automatic uploads on enabled platforms. Use the clip’s Publish dialog for a manual submission, including a previously held manual attempt. Edited files require fresh screening. Sexual-content language holds require editing the flagged text and submitting again.

### Connect publishing accounts

Set app credentials in PowerShell or enter them under **Accounts**, then connect the destinations you want to use. On Windows, the dashboard uses Windows Data Protection API (DPAPI) to encrypt saved credentials and restores them on startup under the same Windows user. On other systems, credentials stay in memory for the session. Reconnect if a provider expires or revokes access. Manual uploads remain available from the clip preview.

- **YouTube** requires YouTube Data API v3 and a Google Cloud Desktop OAuth client. Enter the Client ID and Client Secret in **Accounts → Set up YouTube**, or set `CLIPPER_GOOGLE_CLIENT_ID` and `CLIPPER_GOOGLE_CLIENT_SECRET` in the launching environment. The dashboard uses `http://127.0.0.1:8765/oauth/youtube` as the default loopback callback. Add your Google account as a test user if the consent screen is in testing mode. The upload dialog defaults to private.
- **YouTube via Buffer** uses Buffer Free and Cloudinary Free. Connect your YouTube channel in Buffer, then enter the Buffer API key and Cloudinary cloud name, API key and secret in **Accounts → Set up Buffer**. When you approve a clip, Clipper Studio uploads that MP4 to a public Cloudinary URL and sends it to Buffer for automatic publication with the selected YouTube visibility. The dashboard removes its hosted copy after Buffer confirms that YouTube received it. Use **Check Buffer status** for delayed posts. If Buffer reports a failure, check the existing job in Buffer before retrying. Only use footage you are allowed to expose to the media host even when the YouTube upload is private.
- **Instagram and Facebook** use Facebook Login for Business. In **Accounts → Set up Meta**, enter the App ID, Configuration ID and App Secret. For launch-time setup, set `CLIPPER_META_CLIENT_ID`, `CLIPPER_META_CLIENT_SECRET`, and `CLIPPER_META_CONFIG_ID` in the launching environment instead. Configure `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, and `instagram_content_publish`. Use a Page where you have permission to create content. Instagram needs a professional account linked to that Page. The default callback is `http://localhost:8765/oauth/meta`. If the app requires HTTPS, set `CLIPPER_META_REDIRECT_URI` to an approved HTTPS callback that forwards only `/oauth/meta` to the dashboard. Meta may require app roles or approved access for the accounts you connect.

Use the Publishing tab to review uploads and platform responses. An interrupted or ambiguous submission requires checking the platform before another upload. Campaign submissions, automatic streamer discovery, cross-stream ranking, and post engagement analytics are outside this dashboard's current scope.

If Meta completes sign-in but the dashboard cannot discover your Page, set `CLIPPER_META_PAGE_ID` in the launching environment to the Facebook Page ID you selected during consent. Clipper Studio then requests that Page and its linked Instagram account directly from Meta.
