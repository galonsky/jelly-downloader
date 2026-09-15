FROM python:3.13-slim

ARG DENO_VERSION=2.9.6
ARG DENO_ARCH=x86_64-unknown-linux-gnu
ARG DENO_SHA256=394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/srv/media/videos \
    STATE_DIR=/var/lib/video-downloader

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python - "$DENO_VERSION" "$DENO_ARCH" "$DENO_SHA256" <<'PY'
import hashlib
import io
import sys
import urllib.request
import zipfile

version, architecture, expected_sha256 = sys.argv[1:]
archive_url = f"https://github.com/denoland/deno/releases/download/v{version}/deno-{architecture}.zip"
archive = urllib.request.urlopen(archive_url).read()
actual_sha256 = hashlib.sha256(archive).hexdigest()
if actual_sha256 != expected_sha256:
    raise SystemExit(f"Deno checksum mismatch: {actual_sha256} != {expected_sha256}")
with zipfile.ZipFile(io.BytesIO(archive)) as package:
    package.extract("deno", "/usr/local/bin")
PY
RUN chmod 0755 /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default]==2026.8.19"
COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/media/videos /var/lib/video-downloader \
    && chown -R appuser:appuser /app /srv/media/videos /var/lib/video-downloader

USER appuser
EXPOSE 8080
CMD ["uvicorn", "app.jellyfin_entrypoint:app", "--host", "0.0.0.0", "--port", "8080"]
