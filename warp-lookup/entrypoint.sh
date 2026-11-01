#!/bin/sh
# Bring WARP up (best effort) before starting the lookup app.
# Honest contract: if WARP fails here, the app still starts, but
# /health reports warp=false so a lookup result is never mistaken
# for a WARP result. Render free may block WireGuard UDP — that is
# one of the things the proof test is designed to discover.
set -u
export WARP_UP=false
export WARP_INTERFACE=""

if [ "${WARP_ENABLED:-1}" = "1" ]; then
  if command -v wgcf >/dev/null 2>&1 && command -v wg-quick >/dev/null 2>&1; then
    mkdir -p /tmp/warp
    cd /tmp/warp || exit 1
    if [ ! -f wgcf-account.toml ]; then
      wgcf register --accept-tos >/tmp/warp/register.log 2>&1 || true
    fi
    if [ ! -f wgcf-profile.conf ]; then
      wgcf generate >/tmp/warp/generate.log 2>&1 || true
    fi
    if [ -f wgcf-profile.conf ]; then
      # Route only lookup traffic through WARP in principle: wg-quick
      # brings the interface up; the app itself only ever calls
      # youtubei / googlevideo hostnames for metadata, never video
      # bytes, so no video traffic crosses WARP.
      cp wgcf-profile.conf warp.conf
      wg-quick up warp >/tmp/warp/up.log 2>&1 && {
        export WARP_UP=true
        export WARP_INTERFACE="warp"
      } || true
    fi
    cd /app || exit 1
  else
    echo "warp: wgcf or wg-quick missing, starting without WARP" >&2
  fi
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
