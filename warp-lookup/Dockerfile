FROM python:3.12-slim
# wireguard-tools provides wg-quick; wgcf is PINNED to v2.2.22
# (verified 2026-11-01: this URL returns a 302 to a real binary).
# Do NOT switch back to "latest" — an unpinned build can silently
# fetch a different asset and break the proof test.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wireguard-tools iproute2 curl ca-certificates gnupg lsb-release \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sL https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_linux_amd64 \
       -o /usr/local/bin/wgcf \
    && chmod +x /usr/local/bin/wgcf \
    && /usr/local/bin/wgcf --help >/dev/null
# Cloudflare's own warp-cli (userspace proxy mode, no kernel module,
# no TUN device, no Go build). The final step checks only that the
# binary exists (command -v), NOT a --help/--version flag — wgcf
# v2.2.22 taught us that guessing a flag breaks the whole build.
RUN curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/cloudflare-client.list \
    && apt-get update && apt-get install -y --no-install-recommends cloudflare-warp \
    && rm -rf /var/lib/apt/lists/* \
    && command -v warp-cli
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# PO token provider, PINNED to v0.8.1 (verified 2026-11-01: binary
# generates a real PO token for Me at the zoo, server /ping returns
# version 0.8.1). Do NOT use "latest" — same lesson as wgcf.
# Binary: single file, ~49 MB, no runtime dependencies.
# Plugin: zip with yt_dlp_plugins/extractor/getpot_bgutil*.py, the
# HTTP provider the app calls via extractor_args youtubepot-bgutilhttp.
RUN curl -sL https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/v0.8.1/bgutil-pot-linux-x86_64 \
       -o /usr/local/bin/bgutil-pot \
    && chmod +x /usr/local/bin/bgutil-pot \
    && /usr/local/bin/bgutil-pot --help >/dev/null \
    && mkdir -p /app/plugins \
    && curl -sL https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/download/v0.8.1/bgutil-ytdlp-pot-provider-rs.zip \
       -o /tmp/pot-plugin.zip \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/pot-plugin.zip').extractall('/app/plugins')" \
    && ls /app/plugins/yt_dlp_plugins/extractor/ \
    && rm /tmp/pot-plugin.zip
COPY app.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
