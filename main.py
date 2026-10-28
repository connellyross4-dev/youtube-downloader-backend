"""YouTube downloader prototype backend — v2.

What this does:
  1. POST /api/extract — uses yt-dlp with download=False to get a video's
     title, thumbnail, duration and the available formats. No video file
     is downloaded to this server.
  2. GET /api/download — AUDIO path (unchanged from v1). Re-extracts
     the video, finds the chosen audio format, and returns an HTTP
     redirect to YouTube's own direct media URL (googlevideo). The
     visitor's browser then downloads straight from YouTube's
     servers, not through this server.
  3. GET /api/download-mp4 — MP4 path (new in v2). Downloads the best
     video-only stream (720p max) plus the best audio-only stream to
     a temporary directory, merges them with ffmpeg via yt-dlp into
     a proper MP4 with sound, streams that file to the visitor, and
     deletes the whole temporary directory immediately afterwards.
     No downloaded video is kept in the workspace.

What v2 deliberately does NOT do:
  - No playlists, no age-restricted videos, no live streams.
  - No MP4 for videos over 20 minutes (see MAX_MERGE_DURATION_SECONDS).
  - No server-side MP3 transcoding. The "Audio" option still returns
    YouTube's best audio-only stream (usually .m4a or .webm).
  - No passwords, cookies, or credentials. Do not add any to this file.

Cost warning: the MP4 path is NOT $0 at real traffic. The server pays
download-in (video + audio from YouTube) plus download-out (the merged
MP4 to the visitor) plus CPU for merging, so it needs a bigger host
than Render free (512 MB) once traffic grows. See README v2 notes.

Run locally (ffmpeg must be installed for the MP4 path):
  pip install -r requirements.txt
  TMPDIR=$HOME/.merge-tmp uvicorn main:app --host 127.0.0.1 --port 8000
  # TMPDIR matters: /tmp on some machines is a small tmpfs. Point it at
  # a disk with at least 2 GB free, or the merge endpoint refuses.
"""

import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

app = FastAPI(title="YouTube Downloader Prototype")

# Prototype CORS: the static frontend is on a different origin in
# production (Cloudflare Pages). Restrict this in production by setting
# FRONTEND_ORIGIN to the exact frontend URL and editing this list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# --- Simple per-IP rate limiting (in-memory, per process) -------------
# Render free sleeps and restarts, so this is a guard against casual
# abuse, not a production-grade limiter. A production site needs a
# shared store (e.g. Redis) once it runs on more than one instance.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 10
_hits: dict[str, deque] = defaultdict(deque)
_hits_lock = threading.Lock()


def check_rate_limit(client_ip: str) -> None:
    now = time.time()
    with _hits_lock:
        queue = _hits[client_ip]
        while queue and queue[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            queue.popleft()
        if len(queue) >= RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RATE_LIMITED",
                    "message": (
                        "Too many requests from this address. "
                        "Wait a minute and try again."
                    ),
                },
            )
        queue.append(now)


# --- MP4 merge limits (v2) ----------------------------------------------
# Why 20 minutes: a 720p video is roughly 10-25 MB per minute depending
# on the video, so 20 minutes is roughly 200-500 MB of video + audio
# in, plus a similar merged file out, plus ffmpeg working time. That
# keeps one job inside a small host's disk, memory and timeout budget
# and stops one long video from blocking every other user. Longer
# videos scale cost and time roughly linearly, so raising the cap is
# a paid-host decision, not a free change.
MAX_MERGE_DURATION_SECONDS = 20 * 60
# Stricter than the audio/extract limiter: merging costs bandwidth and
# CPU, so an IP gets only a few merges per hour, not per minute.
MERGE_RATE_LIMIT_WINDOW_SECONDS = 3600
MERGE_RATE_LIMIT_MAX_REQUESTS = 3
_merge_hits: dict[str, deque] = defaultdict(deque)
_merge_hits_lock = threading.Lock()
# One merge at a time. This box and a small paid host have limited CPU;
# two ffmpeg merges at once would slow both and risk running out of
# memory/disk. Extra callers get MERGE_BUSY instead of queueing.
_merge_semaphore = asyncio.Semaphore(1)
MERGE_TIMEOUT_SECONDS = 900
# Refuse before starting if the temp filesystem cannot hold the inputs
# plus the merged output for a 20-minute video.
MERGE_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


def check_merge_rate_limit(client_ip: str) -> None:
    now = time.time()
    with _merge_hits_lock:
        queue = _merge_hits[client_ip]
        while queue and queue[0] <= now - MERGE_RATE_LIMIT_WINDOW_SECONDS:
            queue.popleft()
        if len(queue) >= MERGE_RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "MERGE_RATE_LIMITED",
                    "message": (
                        "Too many MP4 merges from this address. "
                        "MP4 merging is limited to 3 per hour per address. "
                        "Try again later, or use Audio."
                    ),
                },
            )
        queue.append(now)


def check_merge_disk_space() -> None:
    free = shutil.disk_usage(tempfile.gettempdir()).free
    if free < MERGE_MIN_FREE_BYTES:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DISK_FULL",
                "message": (
                    "Not enough temporary disk space on the server to "
                    "merge this video safely. Try again later."
                ),
            },
        )


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- YouTube URL validation -------------------------------------------
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_youtube_url(raw_url: str) -> str:
    """Return a canonical watch URL, or raise a 400 error.

    Rejects playlists outright: if a `list=` parameter is present, v1
    refuses, even if a video id is also present.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_URL", "message": "Paste a YouTube video URL."},
        )
    url = raw_url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_URL", "message": "That URL could not be read."},
        )
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_URL", "message": "Use a full https:// YouTube link."},
        )
    if parsed.netloc.lower() not in _YOUTUBE_HOSTS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NOT_YOUTUBE",
                "message": "This prototype only accepts YouTube video links.",
            },
        )
    query = parse_qs(parsed.query)
    if "list" in query:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PLAYLIST_NOT_SUPPORTED",
                "message": "Playlists are not supported. Paste a single video link.",
            },
        )
    video_id = None
    if parsed.netloc.lower() in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else None
    else:
        ids = query.get("v")
        if ids:
            video_id = ids[0]
        else:
            # /shorts/<id> and /embed/<id>
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                video_id = parts[1]
    if not video_id or not _VIDEO_ID_RE.match(video_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_URL",
                "message": "Could not find a valid 11-character YouTube video id in that link.",
            },
        )
    return f"https://www.youtube.com/watch?v={video_id}"


# --- yt-dlp extraction --------------------------------------------------
def _ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        # Do not use cookies or credentials in v1. That also means some
        # videos YouTube gates behind sign-in will fail honestly here.
        "socket_timeout": 30,
        "retries": 2,
    }


def extract_info(canonical_url: str) -> dict:
    """Extract metadata only. Never downloads the media file."""
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise _map_download_error(str(exc))
    except Exception as exc:  # noqa: BLE001 - surface as a clean API error
        raise HTTPException(
            status_code=502,
            detail={
                "code": "EXTRACTION_FAILED",
                "message": f"Could not read that video. ({str(exc)[:200]})",
            },
        )
    if not info or info.get("_type") == "playlist":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PLAYLIST_NOT_SUPPORTED",
                "message": "Playlists are not supported in v1.",
            },
        )
    age_limit = info.get("age_limit") or 0
    if age_limit and age_limit > 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AGE_RESTRICTED_NOT_SUPPORTED",
                "message": "Age-restricted videos are not supported in v1.",
            },
        )
    if info.get("is_live"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "LIVE_NOT_SUPPORTED",
                "message": "Live streams are not supported in v1.",
            },
        )
    return info


def _map_download_error(message: str) -> HTTPException:
    lowered = message.lower()
    if any(
        token in lowered
        for token in (
            "sign in to confirm",
            "bot",
            "403",
            "429",
            "too many requests",
            "confirm you are not a bot",
        )
    ):
        return HTTPException(
            status_code=502,
            detail={
                "code": "YOUTUBE_BLOCKED",
                "message": (
                    "YouTube blocked this request from the server "
                    "(bot check / rate limit). Try again later, or try "
                    "from a different server IP. This is the most common "
                    "failure mode for free-tier downloader backends."
                ),
            },
        )
    if any(token in lowered for token in ("private video", "video unavailable", "removed", "not found")):
        return HTTPException(
            status_code=404,
            detail={
                "code": "VIDEO_UNAVAILABLE",
                "message": "That video is private, removed, or unavailable.",
            },
        )
    # Sanitise: never echo a full yt-dlp traceback or any credential.
    return HTTPException(
        status_code=502,
        detail={
            "code": "EXTRACTION_FAILED",
            "message": f"Could not read that video. ({message[:200]})",
        },
    )


def _combined_formats(info: dict) -> list[dict]:
    """Combined video+audio formats, 720p max, highest first."""
    out = []
    seen = set()
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") in (None, "none") or fmt.get("acodec") in (None, "none"):
            continue
        height = fmt.get("height") or 0
        if height > 720:
            continue
        url = fmt.get("url")
        if not url:
            continue
        key = (height, fmt.get("ext"))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "format_id": fmt.get("format_id"),
                "label": f"{height}p" if height else "Video",
                "ext": fmt.get("ext"),
                "height": height,
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            }
        )
    out.sort(key=lambda f: f["height"], reverse=True)
    return out


def _audio_formats(info: dict) -> list[dict]:
    out = []
    seen = set()
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") not in (None, "none"):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        url = fmt.get("url")
        if not url:
            continue
        key = fmt.get("ext")
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "format_id": fmt.get("format_id"),
                "label": f"Audio ({fmt.get('ext', 'audio')})",
                "ext": fmt.get("ext"),
                "height": 0,
                "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
                "abr": fmt.get("abr"),
            }
        )
    out.sort(key=lambda f: (f.get("abr") or 0), reverse=True)
    return out


def _merge_video_to_mp4(canonical_url: str, video_id: str) -> tuple[Path, Path]:
    """Download video-only (720p max) + audio, merge to MP4, return paths.

    Returns (tmpdir, merged_mp4_path). The caller MUST delete tmpdir
    after the response is sent, even on error paths inside the caller.
    Raises HTTPException on failure; tmpdir is cleaned up here on
    failure, so the caller only owns it on success.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="ytmerge_"))
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 60,
            "retries": 2,
            # Best video-only at 720p max + best audio; fall back to the
            # best combined format at 720p if a video still has one.
            # yt-dlp merges the two streams with ffmpeg to MP4.
            "format": (
                "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
                "bv*[height<=720]+ba/"
                "b[height<=720]"
            ),
            "merge_output_format": "mp4",
            "outtmpl": str(tmpdir / "%(id)s.%(ext)s"),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([canonical_url])
        mp4_files = sorted(tmpdir.glob("*.mp4"))
        if not mp4_files:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "MERGE_FAILED",
                    "message": (
                        "Merging finished but no MP4 file was produced. "
                        "Try again, or use Audio."
                    ),
                },
            )
        # If several files somehow remain, take the largest MP4.
        merged = max(mp4_files, key=lambda p: p.stat().st_size)
        if merged.stat().st_size == 0:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "MERGE_FAILED",
                    "message": "Merging produced an empty MP4 file. Try again.",
                },
            )
        return tmpdir, merged
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise _map_download_error(str(exc))
    except Exception as exc:  # noqa: BLE001 - surface as a clean API error
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "MERGE_FAILED",
                "message": f"Could not merge that video. ({str(exc)[:200]})",
            },
        )


class ExtractRequest(BaseModel):
    url: str
    kind: str = "mp4"  # "mp4" or "audio"


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "youtube-downloader-prototype",
        "version": "v2",
        "mp4_merge": True,
        "mp4_max_duration_seconds": MAX_MERGE_DURATION_SECONDS,
    }


@app.post("/api/extract")
def extract(request: Request, body: ExtractRequest):
    check_rate_limit(client_ip(request))
    if body.kind not in ("mp4", "audio"):
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_FORMAT", "message": "Choose MP4 or Audio."},
        )
    canonical = parse_youtube_url(body.url)
    info = extract_info(canonical)
    if body.kind == "mp4":
        # v2: MP4 no longer needs a combined format. The server merges
        # video-only + audio-only streams, so NO_COMBINED_FORMAT from
        # v1 no longer applies. The only extract-time MP4 gate is
        # duration (see the merge endpoint for the other gates).
        duration = info.get("duration")
        if duration and duration > MAX_MERGE_DURATION_SECONDS:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "DURATION_TOO_LONG",
                    "message": (
                        "MP4 merging is limited to videos up to 20 minutes "
                        "in v2. This video is longer than that. Try Audio, "
                        "or a shorter video."
                    ),
                },
            )
        formats = [
            {
                "format_id": "MERGED_MP4",
                "label": "720p MP4 (merged, with sound)",
                "ext": "mp4",
                "height": 720,
                "filesize": None,
            }
        ]
    else:
        formats = _audio_formats(info)
        if not formats:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "NO_FORMAT",
                    "message": "No suitable audio format was found for that video.",
                },
            )
    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "video_id": info.get("id"),
        "canonical_url": canonical,
        "kind": body.kind,
        "formats": formats,
        "note": (
            "Audio is YouTube's audio-only stream, not a server-side "
            "MP3 transcode. See README."
            if body.kind == "audio"
            else (
                "MP4 in v2 is merged on the server (720p max, up to "
                "20 minutes). Downloading and merging take a while, "
                "and the server deletes the temporary files right after."
            )
        ),
    }


@app.get("/api/download")
def download(
    request: Request,
    url: str = Query(...),
    format_id: str = Query(...),
    kind: str = Query("mp4"),
):
    """Redirect the visitor's browser to the direct media URL.

    The server never downloads or re-serves the file itself.
    """
    check_rate_limit(client_ip(request))
    canonical = parse_youtube_url(url)
    info = extract_info(canonical)
    target = None
    for fmt in info.get("formats") or []:
        if str(fmt.get("format_id")) == str(format_id) and fmt.get("url"):
            target = fmt["url"]
            break
    if not target:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "FORMAT_NOT_FOUND",
                "message": "That format is no longer available. Extract again.",
            },
        )
    # Direct links expire, so this redirect must be followed immediately.
    return RedirectResponse(url=target, status_code=307)


@app.get("/api/download-mp4")
async def download_mp4(request: Request, url: str = Query(...)):
    """v2 MP4 path: merge video-only + audio on the server, then stream.

    Flow: validate -> extract metadata only -> duration / disk gates
    -> merge (one job at a time, 15-minute timeout) -> stream the
    merged MP4 -> delete the whole temp directory in the background.
    Nothing is kept afterwards.
    """
    ip = client_ip(request)
    check_merge_rate_limit(ip)
    canonical = parse_youtube_url(url)
    info = extract_info(canonical)
    duration = info.get("duration")
    if duration and duration > MAX_MERGE_DURATION_SECONDS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DURATION_TOO_LONG",
                "message": (
                    "MP4 merging is limited to videos up to 20 minutes "
                    "in v2. Try Audio, or a shorter video."
                ),
            },
        )
    check_merge_disk_space()
    video_id = info.get("id") or "video"
    # One merge at a time, without queueing: a busy server says so
    # plainly instead of making a visitor wait behind several merges.
    # NOTE: do NOT use wait_for(acquire(), timeout=0) here — with a
    # zero timeout it cancels before acquire can complete, so every
    # request looked busy. locked() + acquire() with no await between
    # them is the correct non-blocking check in a single async task.
    if _merge_semaphore.locked():
        raise HTTPException(
            status_code=429,
            detail={
                "code": "MERGE_BUSY",
                "message": (
                    "Another MP4 merge is running right now. "
                    "Wait a few minutes and try again."
                ),
            },
        )
    await _merge_semaphore.acquire()
    try:
        loop = asyncio.get_running_loop()
        try:
            tmpdir, merged = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _merge_video_to_mp4, canonical, video_id
                ),
                timeout=MERGE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail={
                    "code": "MERGE_TIMEOUT",
                    "message": (
                        "Merging took longer than 15 minutes and was "
                        "stopped. Try a shorter video, or try again later."
                    ),
                },
            )
    finally:
        _merge_semaphore.release()
    filename = f"{video_id}_720p.mp4"
    return FileResponse(
        path=str(merged),
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(shutil.rmtree, tmpdir, True),
    )
