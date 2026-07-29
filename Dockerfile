FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOAD_DIR=/srv/media/videos \
    STATE_DIR=/var/lib/video-downloader

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt yt-dlp==2026.7.4
COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/media/videos /var/lib/video-downloader \
    && chown -R appuser:appuser /app /srv/media/videos /var/lib/video-downloader

USER appuser
EXPOSE 8080
CMD ["uvicorn", "app.jellyfin_entrypoint:app", "--host", "0.0.0.0", "--port", "8080"]
