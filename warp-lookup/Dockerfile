FROM python:3.12-slim
# wireguard-tools provides wg-quick; wgcf is PINNED to v2.2.22
# (verified 2026-11-01: this URL returns a 302 to a real binary).
# Do NOT switch back to "latest" — an unpinned build can silently
# fetch a different asset and break the proof test.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wireguard-tools iproute2 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sL https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_linux_amd64 \
       -o /usr/local/bin/wgcf \
    && chmod +x /usr/local/bin/wgcf \
    && /usr/local/bin/wgcf --version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
CMD ["./entrypoint.sh"]
