"""Optional Jellyfin refresh wrapper for the downloader worker."""

import os
import threading
import time
import urllib.error
import urllib.request

from . import main


JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
REFRESH_ENABLED = os.getenv("JELLYFIN_REFRESH_ON_COMPLETE", "false").lower() == "true"
REFRESH_INTERVAL_SECONDS = float(os.getenv("JELLYFIN_MIN_REFRESH_INTERVAL_SECONDS", "60"))
_last_refresh = 0.0
_refresh_lock = threading.Lock()
_original_run_download = main.run_download


def refresh_library() -> None:
    """Request a full Jellyfin scan, at most once per configured interval."""
    global _last_refresh
    if not (REFRESH_ENABLED and JELLYFIN_URL and JELLYFIN_API_KEY):
        return
    with _refresh_lock:
        if time.monotonic() - _last_refresh < REFRESH_INTERVAL_SECONDS:
            return
        request = urllib.request.Request(
            f"{JELLYFIN_URL}/Library/Refresh",
            method="POST",
            headers={"X-Emby-Token": JELLYFIN_API_KEY},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"Jellyfin returned HTTP {response.status}")
        except urllib.error.URLError as error:
            raise RuntimeError(f"Jellyfin refresh request failed: {error.reason}") from error
        _last_refresh = time.monotonic()


def run_download(job: dict) -> None:
    _original_run_download(job)
    completed = main.get_job(job["id"])
    if not completed or completed["status"] != "completed":
        return
    try:
        refresh_library()
    except RuntimeError as error:
        # A completed video remains usable if Jellyfin is temporarily unavailable.
        main.update_job(job["id"], error=str(error)[:500])


main.run_download = run_download
app = main.app
