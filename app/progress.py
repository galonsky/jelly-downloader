"""Structured progress parsing for yt-dlp and ffmpeg.

yt-dlp is asked to print machine-readable lines (tab separated, JSON values),
which this module turns into an overall, phase-aware progress snapshot. It does
no I/O so it can be unit tested in isolation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Line prefixes emitted by the yt-dlp command in app.main.run_download.
PROGRESS = "PROGRESS"
PLAN = "PLAN"
TITLE = "TITLE"
FILEPATH = "FILEPATH"

DOWNLOADING = "downloading"
PROCESSING = "processing"
CONVERTING = "converting"


@dataclass
class Event:
    kind: str
    format_id: str | None = None
    data: dict = field(default_factory=dict)
    value: object = None


def _json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None  # yt-dlp prints NA for unavailable template fields.


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_line(line: str) -> Event | None:
    """Parse one line of yt-dlp output; return None for anything unrecognised."""
    kind, _, rest = line.rstrip("\r\n").partition("\t")
    if kind == PROGRESS:
        format_id, _, payload = rest.partition("\t")
        data = _json(payload)
        fid = _json(format_id)
        return Event(PROGRESS, format_id=str(fid) if fid is not None else None, data=data if isinstance(data, dict) else {})
    if kind == PLAN:
        formats = _json(rest)
        return Event(PLAN, value=[f for f in formats if isinstance(f, dict)] if isinstance(formats, list) else [])
    if kind == TITLE:
        title = _json(rest)
        return Event(TITLE, value=title if isinstance(title, str) and title else None)
    if kind == FILEPATH and rest:
        return Event(FILEPATH, value=rest)
    return None


def part_fraction(data: dict) -> float | None:
    """Fraction (0-1) of a single file download, or None when it can't be known."""
    if data.get("status") == "finished":
        return 1.0
    downloaded = _number(data.get("downloaded_bytes"))
    for key in ("total_bytes", "total_bytes_estimate"):
        total = _number(data.get(key))
        if downloaded is not None and total:
            return min(1.0, max(0.0, downloaded / total))
    index, count = _number(data.get("fragment_index")), _number(data.get("fragment_count"))
    if index is not None and count:
        return min(1.0, max(0.0, index / count))
    return None


class ProgressTracker:
    """Combine per-file download progress and post-processing into one snapshot.

    A merged download (``bv*+ba``) fetches several files one after another; each
    is weighted by its expected size (or equally when sizes are unknown) so the
    overall percentage climbs once from 0 to 100 instead of once per file.
    """

    def __init__(self) -> None:
        self.weights: dict[str, float] = {}
        self.completed: set[str] = set()
        self.phase = DOWNLOADING
        self.progress = 0.0
        self.indeterminate = False
        self.speed: float | None = None
        self.eta: int | None = None

    def plan(self, formats: list[dict]) -> None:
        ids = [str(f["format_id"]) for f in formats if f.get("format_id") is not None]
        if len(ids) < 2:
            return  # A single file: weights are implicit.
        sizes = [_number(f.get("filesize")) or _number(f.get("filesize_approx")) for f in formats if f.get("format_id") is not None]
        if all(sizes):
            total = sum(sizes)
            self.weights = {fid: size / total for fid, size in zip(ids, sizes)}
        else:
            self.weights = {fid: 1 / len(ids) for fid in ids}

    def download(self, format_id: str | None, data: dict) -> None:
        self._set_phase(DOWNLOADING)
        fraction = part_fraction(data)
        self.speed = _number(data.get("speed"))
        eta = _number(data.get("eta"))
        self.eta = int(eta) if eta is not None else None
        if self.weights and format_id in self.weights:
            done = sum(self.weights[f] for f in self.completed if f != format_id)
            if fraction is None:
                # Keep what we know; show the bar as busy rather than resetting it.
                self.indeterminate = not self.completed
                overall = done
            else:
                self.indeterminate = False
                overall = done + self.weights[format_id] * fraction
            if data.get("status") == "finished":
                self.completed.add(format_id)
        else:
            self.indeterminate = fraction is None
            overall = fraction or 0.0
        # Estimates can shrink; never let the bar move backwards within a phase.
        self.progress = max(self.progress, min(100.0, overall * 100))

    def set_phase(self, phase: str, percent: float | None = None) -> None:
        self._set_phase(phase)
        self.speed = self.eta = None
        self.indeterminate = percent is None
        if percent is not None:
            self.progress = max(self.progress, min(100.0, max(0.0, percent)))

    def _set_phase(self, phase: str) -> None:
        if phase != self.phase:
            self.phase = phase
            self.progress = 0.0

    def snapshot(self) -> dict:
        return {
            "phase": self.phase,
            "progress": round(self.progress, 1),
            "progress_indeterminate": int(self.indeterminate),
            "speed": self.speed,
            "eta": self.eta,
        }


def ffmpeg_percent(line: str, duration: float | None) -> float | None:
    """Percent complete from an ffmpeg ``-progress`` ``out_time_us=`` line."""
    key, _, value = line.strip().partition("=")
    if key != "out_time_us" or not duration or duration <= 0:
        return None
    try:
        microseconds = int(value)
    except ValueError:
        return None  # ffmpeg prints N/A before the first frame.
    return min(100.0, max(0.0, microseconds / 1_000_000 / duration * 100))
