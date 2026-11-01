#!/bin/sh
# Bring WARP up (best effort) before starting the lookup app.
# Two routes, in order, one variable at a time:
#  1. Kernel WireGuard via wgcf + wg-quick (already tried on Render
#     2026-11-01, failed: /health said warp=false, no reason logged
#     in /health. This version logs the reason to /tmp/warp/status.txt
#     and exposes it in /health as warp_error, so a failure is
#     diagnostic, not just false.)
#  2. Cloudflare's own warp-cli in PROXY mode (userspace, no kernel
#     module, no TUN device, no Go build). warp-svc runs in the
#     background, warp-cli connects, and a SOCKS5 proxy listens on
#     127.0.0.1:40000. The app then routes ONLY yt-dlp lookup
#     traffic through that proxy. No video bytes ever cross WARP.
# Honest contract: if both fail, the app still starts, but /health
# reports warp=false, warp_mode=none, and warp_error, so a lookup
# result is never mistaken for a WARP result.
set -u
export WARP_UP=false
export WARP_INTERFACE=""
export WARP_MODE="none"
export WARP_PROXY_URL=""
export WARP_ERROR="not attempted"

mkdir -p /tmp/warp

if [ "${WARP_ENABLED:-1}" = "1" ]; then
  # Route 1: kernel WireGuard
  if command -v wgcf >/dev/null 2>&1 && command -v wg-quick >/dev/null 2>&1; then
    cd /tmp/warp || exit 1
    if [ ! -f wgcf-account.toml ]; then
      wgcf register --accept-tos >/tmp/warp/register.log 2>&1 || true
    fi
    if [ ! -f wgcf-profile.conf ]; then
      wgcf generate >/tmp/warp/generate.log 2>&1 || true
    fi
    if [ -f wgcf-profile.conf ]; then
      cp wgcf-profile.conf warp.conf
      if wg-quick up warp >/tmp/warp/up.log 2>&1; then
        export WARP_UP=true
        export WARP_INTERFACE="warp"
        export WARP_MODE="kernel"
        export WARP_ERROR=""
      else
        export WARP_ERROR="wg-quick failed: $(head -c 300 /tmp/warp/up.log 2>/dev/null | tr '\n' ' ')"
      fi
    else
      export WARP_ERROR="wgcf generate failed, no wgcf-profile.conf"
    fi
    cd /app || exit 1
  else
    export WARP_ERROR="wgcf or wg-quick missing"
  fi

  # Route 2: warp-cli proxy mode, only if kernel route failed
  if [ "$WARP_UP" = "false" ] && command -v warp-cli >/dev/null 2>&1; then
    # warp-svc is the daemon; run it in the background, best effort.
    (warp-svc >/tmp/warp/svc.log 2>&1 &) || true
    sleep 3
    warp-cli register >/tmp/warp/cli-register.log 2>&1 || true
    warp-cli set-mode proxy >/tmp/warp/cli-mode.log 2>&1 || true
    warp-cli connect >/tmp/warp/cli-connect.log 2>&1 || true
    # Wait up to 30s for Connected, checking every 3s.
    i=0
    while [ $i -lt 10 ]; do
      if warp-cli status 2>/dev/null | grep -qi "Connected"; then
        break
      fi
      sleep 3
      i=$((i+1))
    done
    if warp-cli status 2>/dev/null | grep -qi "Connected"; then
      # Proxy should listen on 127.0.0.1:40000 in proxy mode.
      if curl -s --max-time 10 --socks5-hostname 127.0.0.1:40000 https://api.ipify.org >/tmp/warp/proxy-ip.txt 2>/tmp/warp/proxy-ip.log; then
        export WARP_UP=true
        export WARP_MODE="proxy"
        export WARP_PROXY_URL="socks5://127.0.0.1:40000"
        export WARP_ERROR=""
      else
        export WARP_ERROR="warp-cli Connected but SOCKS proxy test failed"
      fi
    else
      export WARP_ERROR="warp-cli did not reach Connected in 30s"
    fi
  fi
fi

# Persist status for /health (env is also passed to uvicorn below).
printf '%s' "$WARP_UP" > /tmp/warp/status.txt
printf '%s' "$WARP_MODE" > /tmp/warp/mode.txt
printf '%s' "$WARP_ERROR" > /tmp/warp/error.txt
printf '%s' "$WARP_PROXY_URL" > /tmp/warp/proxy.txt

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
