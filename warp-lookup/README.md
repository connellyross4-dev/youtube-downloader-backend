# ClipSaver WARP lookup — deploy authorized 2026-11-01

Status (2026-11-01 11:25 EAT): Osmish said "yes go make no mistake
build everything carefully". Code was re-checked, fixed, and tested
locally that day: py_compile OK, shape test PASS (720p cap enforced,
1080p video-only dropped), video_id test PASS, local /health 200
(warp=false locally, as expected, egress IP reported), bad videoId
400 handled. Pinned wgcf v2.2.22, added egress IP to /health.

Deploy step (authorized):
1. Push this folder to the public repo `connellyross4-dev/youtube-downloader-backend`
   — never touch the old GitHub account osmish2 (hard rule 2026-10-28).
2. Render → Public Git Repository tab with that repo URL only, Root
   Directory = `warp-lookup`, free plan, no card.

## Why this exists
- 2026-10-31, live POST to `https://clipsaver2.mamesheriff.workers.dev/lookup`
  returned JSON 502 `{"error":"YouTube blocked this lookup (no streamingData
  from Worker)"}` — the Worker code runs, CORS is gone, but YouTube
  blocks the Cloudflare IP. Client-switching across 7 clients was
  tested the same day from a datacenter IP and also failed, so no
  further client list is a fix.
- A Worker cannot run WARP/WireGuard/bgutil. This Docker service can,
  on Render free (the account already used for the prototype backend).

## What it is
Lookup JSON only (~5 KB). The visitor's browser still downloads video
bytes direct from googlevideo; nothing video-sized crosses WARP or
Render. PO token is NOT wired in yet — this is a one-variable WARP-only
test first. If WARP alone still fails, the next step is PO token, not
another client list.

## Deploy step (Osmish's call only)
1. Push this folder to the public repo `connellyross4-dev/youtube-downloader-backend`
   — never touch the old GitHub account osmish2 (hard rule 2026-10-28).
2. Render → Public Git Repository tab with that repo URL only, Root
   Directory = `warp-lookup`, free plan, no card.
3. Check `GET /health` first: it must say `warp: true` before any
   lookup result counts as a WARP result.
4. Proof test: `POST /lookup {"videoId": "jNQXAC9IVRw"}` (Me at the zoo).
   - PASS = formats listed → point the ClipSaver site at this URL.
   - FAIL = still LOGIN_REQUIRED / no streamingData → stop here;
     remaining $0 option is visitor-browser extraction, or a paid
     residential proxy (not $5).

## Known risks, stated before any deploy
- Render free may block WireGuard UDP, in which case /health will say
  warp=false and the test is inconclusive for WARP, not a pass.
- WARP IPs may themselves be flagged in 2026 — the builder report is
  not a guarantee.
- The wgcf download in the Dockerfile fetches "latest" at build time;
  the build log must be checked for the version actually fetched.
- This code has been syntax-checked only (python -m py_compile);
  it has NOT been run against YouTube from a WARP IP — that is
  exactly what the proof test is for.
