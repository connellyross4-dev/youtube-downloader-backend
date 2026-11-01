"""WARP lookup backend — lookup ONLY, no video bytes, no merge.

Purpose: one proof test. The Cloudflare Worker lookup (clipsaver2,
2026-10-31) returned 502 "YouTube blocked this lookup (no
streamingData from Worker)" because its outbound IP is a Cloudflare
datacenter IP. A Worker cannot run WARP. This service runs on a Docker
host (Render free, the account already used for the prototype backend)
where a WARP sidecar can change the outbound IP for the lookup only.

What it does:
  POST /lookup  {"videoId": "jNQXAC9IVRw"}  (or {"url": "..."})
    -> tries yt-dlp player clients in order (android, web, ios,
       tv_embedded), stops at the first one returning formats, and
       returns title/duration plus combined (<=720p), video-only and
       audio-only direct URLs — the same shape the ClipSaver site
       already consumes. Video bytes are NEVER proxied through here:
       the visitor's browser downloads straight from googlevideo.
  GET  /health -> {"status": "ok", "warp": true|false, ...}
    warp=true means the entrypoint brought a WireGuard/WARP interface
    up before the app started. warp=false means the lookup below ran
    over the host's direct IP — a PASS in that state does NOT prove
    WARP works, and a FAIL does not prove WARP fails. Read /health
    before judging any /lookup result.

What it deliberately does NOT do yet:
  - No PO-token provider (bgutil) wired in. This is a one-variable
    test: WARP alone first, because the builder report behind this
    stack said routing through WARP made the bot checks vanish.
    If WARP alone still returns LOGIN_REQUIRED / no streamingData,
    the next step is a PO token — not another client list (client
    switching alone was already tested 2026-10-31 across 7 clients
    and failed on a datacenter IP).
  - No playlists, age-restricted, or live videos. No credentials,
    cookies, or keys in this file. No video download, no merge.

Deploy (only on Osmish's say-so, nothing pushed by this file alone):
  Render -> Public Git Repository tab with the public repo URL only
  (the old GitHub account osmish2 is untouched — hard rule
  2026-10-28), Root Directory = warp-lookup, free plan, no card.
  Then: POST /lookup with Me at the zoo (jNQXAC9IVRw).
    PASS = formats listed.  FAIL = still LOGIN_REQUIRED / no
    streamingData => stop chasing this route; the remaining $0
    option is visitor-browser extraction, or a paid residential
    proxy (not $5).
"""

import os
import re
import urllib.request

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="ClipSaver WARP lookup")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Order mirrors the 2026-10-31 test: ANDROID + WEB were the Worker's
# clients; IOS and TV_EMBEDDED are the remaining fallbacks that at
# least returned a player response shape on some clients.
_CLIENTS = ["android", "web", "ios", "tv_embedded"]


class LookupRequest(BaseModel):
    videoId: str | None = None
    url: str | None = None


def _video_id(body: LookupRequest) -> str:
    if body.videoId and _VIDEO_ID_RE.match(body.videoId.strip()):
        return body.videoId.strip()
    if body.url:
        m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", body.url)
        if m:
            return m.group(1)
    raise HTTPException(status_code=400, detail={"error": "no videoId"})


def _try_client(video_id: str, client: str) -> dict | None:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 1,
        "extractor_args": {"youtube": {"player_client": [client]}},
    }
    # Proxy mode only: route yt-dlp lookup traffic through the local
    # WARP SOCKS5 proxy. Kernel mode needs no proxy option — the
    # WireGuard interface already routes all outbound traffic.
    proxy = os.environ.get("WARP_PROXY_URL", "")
    if proxy:
        opts["proxy"] = proxy
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info or not info.get("formats"):
        return None
    return info


def _shape(info: dict) -> dict:
    combined, video_only, audio_only = [], [], []
    for fmt in info.get("formats") or []:
        url = fmt.get("url")
        if not url:
            continue
        height = fmt.get("height") or 0
        entry = {
            "format_id": fmt.get("format_id"),
            "label": fmt.get("format_note") or (f"{height}p" if height else "Audio"),
            "ext": fmt.get("ext"),
            "height": height,
            "url": url,
        }
        vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
        if vcodec not in (None, "none") and acodec not in (None, "none"):
            if height <= 720:
                combined.append(entry)
        elif vcodec not in (None, "none"):
            # 720p cap applies to video-only too — the site promises
            # max 720p, and a 1080p video-only file must not leak out.
            if height <= 720:
                video_only.append(entry)
        elif acodec not in (None, "none"):
            audio_only.append(entry)
    # Highest first, and keep the JSON small (~5 KB, lookup only).
    combined.sort(key=lambda f: f["height"], reverse=True)
    video_only.sort(key=lambda f: f["height"], reverse=True)
    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "video_id": info.get("id"),
        "combined": combined[:12],
        "video_only": video_only[:12],
        "audio_only": audio_only[:12],
    }


def _egress_ip() -> str | None:
    """Best-effort outbound IP, for proving WARP vs host IP in /health.

    Never crashes health: any failure returns None.
    """
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            ip = r.read().decode().strip()
            return ip if ip else None
    except Exception:
        return None


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "clipsaver-warp-lookup",
        "warp": os.environ.get("WARP_UP", "false") == "true",
        "warp_mode": os.environ.get("WARP_MODE", "none"),
        "warp_interface": os.environ.get("WARP_INTERFACE", ""),
        "warp_proxy": os.environ.get("WARP_PROXY_URL", ""),
        "warp_error": os.environ.get("WARP_ERROR", ""),
        "tun_available": os.path.exists("/dev/net/tun"),
        "egress_ip": _egress_ip(),
        "note": "warp=false means this lookup ran over the host IP, not WARP.",
    }


@app.post("/lookup")
def lookup(body: LookupRequest):
    video_id = _video_id(body)
    tried = []
    for client in _CLIENTS:
        tried.append(client)
        info = _try_client(video_id, client)
        if info:
            out = _shape(info)
            out["client_used"] = client
            out["clients_tried"] = tried
            out["warp"] = os.environ.get("WARP_UP", "false") == "true"
            out["warp_mode"] = os.environ.get("WARP_MODE", "none")
            return out
    raise HTTPException(
        status_code=502,
        detail={
            "error": "YouTube blocked this lookup (no streamingData from WARP host)",
            "clients_tried": tried,
            "warp": os.environ.get("WARP_UP", "false") == "true",
            "warp_mode": os.environ.get("WARP_MODE", "none"),
            "warp_error": os.environ.get("WARP_ERROR", ""),
        },
    )
