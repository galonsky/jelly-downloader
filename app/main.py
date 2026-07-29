import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl


DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/srv/media/videos"))
STATE_DIR = Path(os.getenv("STATE_DIR", "/var/lib/video-downloader"))
DATABASE_PATH = STATE_DIR / "downloader.db"
POLL_INTERVAL_SECONDS = float(os.getenv("QUEUE_POLL_INTERVAL_SECONDS", "1"))
PROGRESS_RE = re.compile(r"^download:([0-9.]+)%$")

app = FastAPI(title="Video Downloader", docs_url=None, redoc_url=None)


class DownloadRequest(BaseModel):
    url: HttpUrl


def now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_directories() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                title TEXT,
                output_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            )
            """
        )
        # A process was interrupted by a pod restart; safely retry its job.
        connection.execute("UPDATE downloads SET status = 'queued', started_at = NULL WHERE status = 'running'")


def job_from_row(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["output_filename"] = Path(job.pop("output_path")).name if job.get("output_path") else None
    return job


def list_jobs() -> list[dict]:
    with database() as connection:
        rows = connection.execute(
            "SELECT * FROM downloads ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, created_at DESC"
        ).fetchall()
    return [job_from_row(row) for row in rows]


def get_job(job_id: str) -> dict | None:
    with database() as connection:
        row = connection.execute("SELECT * FROM downloads WHERE id = ?", (job_id,)).fetchone()
    return job_from_row(row) if row else None


def update_job(job_id: str, **fields: object) -> None:
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    with database() as connection:
        connection.execute(f"UPDATE downloads SET {columns} WHERE id = ?", (*fields.values(), job_id))


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


def validate_public_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only HTTP(S) URLs are supported")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Local network URLs are not allowed")
    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError("Could not resolve the URL hostname") from error
    if not addresses or any(not is_public_ip(address) for address in addresses):
        raise ValueError("Only publicly routable URL hosts are allowed")


def next_queued_job() -> dict | None:
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM downloads WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        connection.execute(
            "UPDATE downloads SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
            (now(), row["id"]),
        )
        if connection.total_changes < 1:
            return None
        return dict(row)


def media_codecs(path: Path) -> tuple[str | None, str | None]:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout).get("streams", [])
    codecs = {stream.get("codec_type"): stream.get("codec_name") for stream in streams}
    return codecs.get("video"), codecs.get("audio")


def ensure_jellyfin_compatibility(path: Path) -> Path:
    video_codec, audio_codec = media_codecs(path)
    if path.suffix.lower() == ".mp4" and video_codec == "h264" and audio_codec == "aac":
        return path
    compatible = path.with_name(f"{path.stem}.jellyfin.mp4")
    temporary = compatible.with_suffix(".partial.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(temporary)],
        check=True,
        capture_output=True,
        text=True,
    )
    temporary.replace(compatible)
    path.unlink(missing_ok=True)
    return compatible


def run_download(job: dict) -> None:
    job_id = job["id"]
    template = str(DOWNLOAD_DIR / "%(title).200B [%(id)s].%(ext)s")
    command = [
        "yt-dlp", "--no-playlist", "--newline", "--progress",
        "--progress-template", "download:%(progress._percent_str)s",
        "--print", "after_move:filepath:%(filepath)s",
        "--restrict-filenames", "--windows-filenames",
        "-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format", "mp4", "-o", template, job["url"],
    ]
    output_path: Path | None = None
    title: str | None = None
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        line = line.strip()
        progress_match = PROGRESS_RE.match(line)
        if progress_match:
            update_job(job_id, progress=min(100.0, float(progress_match.group(1))))
        elif line.startswith("filepath:"):
            output_path = Path(line.removeprefix("filepath:"))
        elif line.startswith("[download] Destination:"):
            title = Path(line.split(":", 1)[1].strip()).stem
    if process.wait() != 0:
        raise RuntimeError("yt-dlp could not download this video")
    if not output_path or not output_path.exists():
        raise RuntimeError("yt-dlp completed without producing a video file")
    final_path = ensure_jellyfin_compatibility(output_path)
    update_job(job_id, status="completed", progress=100.0, title=title or final_path.stem, output_path=str(final_path), completed_at=now())


def worker() -> None:
    while True:
        job = next_queued_job()
        if not job:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        try:
            run_download(job)
        except Exception as error:  # Keep the worker alive and make failures visible in the UI.
            update_job(job["id"], status="failed", error=str(error)[:500], completed_at=now())


@app.on_event("startup")
def startup() -> None:
    ensure_directories()
    initialize_database()
    threading.Thread(target=worker, name="download-worker", daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/api/downloads")
def downloads() -> list[dict]:
    return list_jobs()


@app.post("/api/downloads", status_code=201)
def create_download(request: DownloadRequest) -> dict:
    value = str(request.url)
    try:
        validate_public_url(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    job_id = str(uuid.uuid4())
    with database() as connection:
        connection.execute(
            "INSERT INTO downloads (id, url, status, created_at) VALUES (?, ?, 'queued', ?)",
            (job_id, value, now()),
        )
    job = get_job(job_id)
    assert job is not None
    return job


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Video Downloader</title>
<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#172033}form{display:flex;gap:.5rem}input{flex:1;padding:.7rem;font:inherit}button{padding:.7rem 1rem;font:inherit}table{width:100%;border-collapse:collapse;margin-top:2rem}th,td{text-align:left;padding:.6rem;border-bottom:1px solid #ddd;vertical-align:top}.error{color:#b42318}.status{font-weight:600}</style>
</head><body><h1>Video Downloader</h1><form id="download-form"><input id="url" type="url" required placeholder="https://example.com/video"><button>Download</button></form><p id="message" role="status"></p><table><thead><tr><th>Status</th><th>Video</th><th>Progress</th><th>Result</th></tr></thead><tbody id="jobs"></tbody></table>
<script>
const form=document.querySelector('#download-form'), message=document.querySelector('#message'), jobs=document.querySelector('#jobs');
function cell(value){const td=document.createElement('td');td.textContent=value||'—';return td}
async function refresh(){const response=await fetch('/api/downloads');if(!response.ok)return;const items=await response.json();jobs.replaceChildren(...items.map(job=>{const tr=document.createElement('tr');const status=cell(job.status);status.className='status';tr.append(status,cell(job.title||job.url),cell(job.status==='completed'?'100%':`${Math.round(job.progress||0)}%`),cell(job.error||job.output_filename));if(job.error)tr.lastChild.className='error';return tr}))}
form.addEventListener('submit',async event=>{event.preventDefault();message.textContent='Queueing download…';const response=await fetch('/api/downloads',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value})});const data=await response.json();message.textContent=response.ok?'Download queued.':data.detail||'Could not queue download.';if(response.ok){form.reset();refresh()}});refresh();setInterval(refresh,1000);
</script></body></html>"""
