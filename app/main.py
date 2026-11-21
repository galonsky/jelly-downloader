import ipaddress
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl

from . import progress as progress_parsing


DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "/srv/media/videos"))
STATE_DIR = Path(os.getenv("STATE_DIR", "/var/lib/video-downloader"))
DATABASE_PATH = STATE_DIR / "downloader.db"
POLL_INTERVAL_SECONDS = float(os.getenv("QUEUE_POLL_INTERVAL_SECONDS", "1"))
PROGRESS_WRITE_INTERVAL_SECONDS = 0.5
PROGRESS_COLUMNS = {
    "phase": "TEXT",
    "speed": "REAL",
    "eta": "INTEGER",
    "progress_indeterminate": "INTEGER NOT NULL DEFAULT 0",
}
RESET_PROGRESS_SQL = "progress = 0, phase = NULL, speed = NULL, eta = NULL, progress_indeterminate = 0"

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
        existing = {row["name"] for row in connection.execute("PRAGMA table_info(downloads)")}
        for name, definition in PROGRESS_COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE downloads ADD COLUMN {name} {definition}")
        # A process was interrupted by a pod restart; safely retry its job.
        connection.execute(
            f"UPDATE downloads SET status = 'queued', started_at = NULL, {RESET_PROGRESS_SQL} WHERE status = 'running'"
        )


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
            f"UPDATE downloads SET status = 'running', started_at = ?, {RESET_PROGRESS_SQL} WHERE id = ? AND status = 'queued'",
            (now(), row["id"]),
        )
        if connection.total_changes < 1:
            return None
        return dict(row)


def probe_media(path: Path) -> tuple[str | None, str | None, float | None]:
    """Return the video codec, audio codec and duration in seconds of a media file."""
    command = [
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    probe = json.loads(result.stdout)
    codecs = {stream.get("codec_type"): stream.get("codec_name") for stream in probe.get("streams", [])}
    try:
        duration = float(probe.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None
    return codecs.get("video"), codecs.get("audio"), duration


@contextmanager
def stopped_on_error(process: subprocess.Popen):
    """Kill and reap a child process if reading its output raises, so it never outlives the job."""
    try:
        yield
    except BaseException:
        process.kill()
        process.wait()
        raise


def ensure_jellyfin_compatibility(path: Path, on_progress: Callable[[float | None], None] | None = None) -> Path:
    video_codec, audio_codec, duration = probe_media(path)
    if path.suffix.lower() == ".mp4" and video_codec == "h264" and audio_codec == "aac":
        return path
    compatible = path.with_name(f"{path.stem}.jellyfin.mp4")
    temporary = compatible.with_suffix(".partial.mp4")
    if on_progress:
        on_progress(0.0 if duration else None)
    command = [
        "ffmpeg", "-y", "-nostats", "-progress", "pipe:1", "-i", str(path), "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(temporary),
    ]
    errors: deque[str] = deque(maxlen=30)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdout is not None and process.stderr is not None
    stderr = process.stderr
    reader = threading.Thread(target=lambda: errors.extend(line.rstrip() for line in stderr), daemon=True)
    reader.start()
    try:
        with stopped_on_error(process):
            for line in process.stdout:
                percent = progress_parsing.ffmpeg_percent(line, duration)
                if percent is not None and on_progress:
                    on_progress(percent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return_code = process.wait()
    reader.join(timeout=5)
    if return_code != 0:
        temporary.unlink(missing_ok=True)
        raise subprocess.CalledProcessError(return_code, command, stderr="\n".join(errors))
    temporary.replace(compatible)
    path.unlink(missing_ok=True)
    return compatible


def yt_dlp_command(url: str) -> list[str]:
    template = str(DOWNLOAD_DIR / "%(title).200B [%(id)s].%(ext)s")
    progress_fields = "status,downloaded_bytes,total_bytes,total_bytes_estimate,fragment_index,fragment_count,speed,eta"
    return [
        "yt-dlp", "--js-runtimes", "deno", "--no-playlist", "--newline", "--progress",
        "--progress-delta", str(PROGRESS_WRITE_INTERVAL_SECONDS),
        # Structured, tab-separated lines parsed by app.progress.parse_line.
        "--progress-template", f"download:{progress_parsing.PROGRESS}\t%(info.format_id)j\t%(progress.{{{progress_fields}}})j",
        "--print", f"before_dl:{progress_parsing.PLAN}\t%(requested_formats.:.{{format_id,filesize,filesize_approx}})j",
        "--print", f"before_dl:{progress_parsing.TITLE}\t%(title)j",
        "--print", f"after_move:{progress_parsing.FILEPATH}\t%(filepath)s",
        "--restrict-filenames", "--windows-filenames",
        "-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format", "mp4", "-o", template, url,
    ]


class ProgressWriter:
    """Persist tracker snapshots, throttled so SQLite isn't written on every line."""

    def __init__(self, job_id: str, tracker: progress_parsing.ProgressTracker) -> None:
        self.job_id = job_id
        self.tracker = tracker
        self.last_write = 0.0
        self.last_phase: str | None = None

    def write(self, force: bool = False) -> None:
        current = time.monotonic()
        if not force and self.tracker.phase == self.last_phase and current - self.last_write < PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        self.last_write, self.last_phase = current, self.tracker.phase
        update_job(self.job_id, **self.tracker.snapshot())


def run_download(job: dict) -> None:
    job_id = job["id"]
    output_path: Path | None = None
    title: str | None = None
    recent_output = deque(maxlen=30)
    tracker = progress_parsing.ProgressTracker()
    writer = ProgressWriter(job_id, tracker)
    writer.write(force=True)
    process = subprocess.Popen(yt_dlp_command(job["url"]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert process.stdout is not None
    with stopped_on_error(process):
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            event = progress_parsing.parse_line(line)
            if event is None:
                if line.strip():
                    recent_output.append(line.strip())
                continue
            if event.kind == progress_parsing.PROGRESS:
                tracker.download(event.format_id, event.data)
                if event.data.get("status") == "finished" and tracker.progress >= 99.9:
                    # All files are on disk; yt-dlp now merges and fixes them up.
                    tracker.set_phase(progress_parsing.PROCESSING)
                writer.write()
            elif event.kind == progress_parsing.PLAN:
                tracker.plan(event.value)
            elif event.kind == progress_parsing.TITLE:
                title = event.value or title
                if title:
                    update_job(job_id, title=title)
            elif event.kind == progress_parsing.FILEPATH:
                output_path = Path(event.value)
    return_code = process.wait()
    if return_code != 0:
        details = "\n".join(recent_output)
        raise RuntimeError(f"yt-dlp failed (exit code {return_code}): {details}"[:2000])
    if not output_path or not output_path.exists():
        raise RuntimeError("yt-dlp completed without producing a video file")

    def converting(percent: float | None) -> None:
        tracker.set_phase(progress_parsing.CONVERTING, percent)
        writer.write()

    final_path = ensure_jellyfin_compatibility(output_path, on_progress=converting)
    update_job(
        job_id, status="completed", progress=100.0, phase=None, speed=None, eta=None, progress_indeterminate=0,
        title=title or final_path.stem, output_path=str(final_path), completed_at=now(),
    )


def worker() -> None:
    while True:
        job = next_queued_job()
        if not job:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        try:
            run_download(job)
        except Exception as error:  # Keep the worker alive and make failures visible in the UI.
            update_job(
                job["id"], status="failed", error=str(error)[:500], completed_at=now(),
                phase=None, speed=None, eta=None, progress_indeterminate=0,
            )


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
<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#172033}form{display:flex;gap:.5rem}input{flex:1;padding:.7rem;font:inherit}button{padding:.7rem 1rem;font:inherit}table{width:100%;border-collapse:collapse;margin-top:2rem}th,td{text-align:left;padding:.6rem;border-bottom:1px solid #ddd;vertical-align:top}td{overflow-wrap:anywhere}.error{color:#b42318}.status{font-weight:600}.progress{min-width:12rem}.progress progress{width:100%;height:.8rem}.progress span{display:block;font-size:.85rem;color:#475467;margin-top:.2rem}</style>
</head><body><h1>Video Downloader</h1><form id="download-form"><input id="url" type="url" required aria-label="Video URL" placeholder="https://example.com/video"><button>Download</button></form><p id="message" role="status"></p><table><thead><tr><th>Status</th><th>Video</th><th>Progress</th><th>Result</th></tr></thead><tbody id="jobs"></tbody></table>
<script>
const form=document.querySelector('#download-form'), message=document.querySelector('#message'), jobs=document.querySelector('#jobs');
const PHASES={downloading:'Downloading',processing:'Processing',converting:'Converting for Jellyfin'};
function bytes(n){const units=['B','KiB','MiB','GiB'];let i=0;while(n>=1024&&i<units.length-1){n/=1024;i++}return `${n.toFixed(i?1:0)} ${units[i]}`}
function duration(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=String(s%60).padStart(2,'0');return h?`${h}:${String(m).padStart(2,'0')}:${sec}`:`${m}:${sec}`}
function progressText(job){
  if(job.status==='completed')return 'Done';
  if(job.status==='queued')return 'Waiting in queue';
  if(job.status!=='running')return `Stopped at ${Math.round(job.progress||0)}%`;
  const parts=[PHASES[job.phase]||'Starting'];
  if(!job.progress_indeterminate&&job.phase)parts[0]+=` ${Math.round(job.progress||0)}%`;else parts[0]+='…';
  if(job.speed)parts.push(`${bytes(job.speed)}/s`);
  if(job.eta!=null&&job.phase==='downloading')parts.push(`ETA ${duration(job.eta)}`);
  return parts.join(' · ');
}
function row(job){
  let tr=jobs.querySelector(`tr[data-id="${job.id}"]`);
  if(!tr){tr=document.createElement('tr');tr.dataset.id=job.id;
    const td=()=>document.createElement('td');const progress=td();progress.className='progress';
    progress.append(document.createElement('progress'),document.createElement('span'));
    tr.append(td(),td(),progress,td());tr.cells[0].className='status'}
  const [status,video,progress,result]=tr.cells;
  status.textContent=job.status;
  video.textContent=job.title||job.url;
  const bar=progress.querySelector('progress'),label=progress.querySelector('span'),text=progressText(job);
  const indeterminate=job.status==='running'&&(job.progress_indeterminate||!job.phase);
  if(indeterminate)bar.removeAttribute('value');else bar.value=job.status==='completed'?100:(job.status==='queued'?0:job.progress||0);
  bar.max=100;bar.setAttribute('aria-label',`${job.title||job.url}: ${text}`);label.textContent=text;
  result.textContent=job.error||job.output_filename||'—';result.className=job.error?'error':'';
  return tr;
}
async function refresh(){const response=await fetch('/api/downloads');if(!response.ok)return;const items=await response.json();const rows=items.map(row);jobs.replaceChildren(...rows)}
form.addEventListener('submit',async event=>{event.preventDefault();message.textContent='Queueing download…';const response=await fetch('/api/downloads',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({url:document.querySelector('#url').value})});const data=await response.json();message.textContent=response.ok?'Download queued.':data.detail||'Could not queue download.';if(response.ok){form.reset();refresh()}});refresh();setInterval(refresh,1000);
</script></body></html>"""
