import pytest

from app.progress import (
    CONVERTING,
    DOWNLOADING,
    PROGRESS,
    ProgressTracker,
    ffmpeg_percent,
    parse_line,
    part_fraction,
)


def test_parse_progress_line_from_yt_dlp():
    event = parse_line('PROGRESS\t"137"\t{"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 4096}\n')
    assert event.kind == PROGRESS
    assert event.format_id == "137"
    assert event.data == {"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 4096}


def test_parse_handles_na_and_garbage():
    assert parse_line("PROGRESS\tNA\tNA").data == {}
    assert parse_line("PROGRESS\tNA\tNA").format_id is None
    assert parse_line("PLAN\tNA").value == []
    assert parse_line("TITLE\tNA").value is None
    assert parse_line("[download]  45.3% of 10MiB") is None
    assert parse_line("") is None


def test_parse_plan_title_and_filepath():
    plan = parse_line('PLAN\t[{"format_id": "137", "filesize": 90}, {"format_id": "140", "filesize": 10}]')
    assert [f["format_id"] for f in plan.value] == ["137", "140"]
    assert parse_line('TITLE\t"A \\"quoted\\" title"').value == 'A "quoted" title'
    assert parse_line("FILEPATH\t/srv/media/videos/a [x].mp4").value == "/srv/media/videos/a [x].mp4"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"downloaded_bytes": 25, "total_bytes": 100}, 0.25),
        ({"downloaded_bytes": 25, "total_bytes_estimate": 50}, 0.5),
        ({"fragment_index": 3, "fragment_count": 12}, 0.25),
        ({"downloaded_bytes": 10}, None),
        ({"status": "finished"}, 1.0),
        ({"downloaded_bytes": 200, "total_bytes": 100}, 1.0),
    ],
)
def test_part_fraction(data, expected):
    assert part_fraction(data) == expected


def test_single_file_reports_intermediate_progress():
    # Regression: the old regex only ever matched "100.0%", so progress showed 0 or 100.
    tracker = ProgressTracker()
    tracker.plan([])
    tracker.download("18", {"status": "downloading", "downloaded_bytes": 453, "total_bytes": 1000})
    snapshot = tracker.snapshot()
    assert snapshot["phase"] == DOWNLOADING
    assert snapshot["progress"] == 45.3
    assert snapshot["progress_indeterminate"] == 0


def test_merged_download_is_weighted_by_size():
    tracker = ProgressTracker()
    tracker.plan([{"format_id": "137", "filesize": 90_000_000}, {"format_id": "140", "filesize_approx": 10_000_000}])
    tracker.download("137", {"status": "downloading", "downloaded_bytes": 45, "total_bytes": 90})
    assert tracker.progress == pytest.approx(45.0)
    tracker.download("137", {"status": "finished", "downloaded_bytes": 90, "total_bytes": 90})
    assert tracker.progress == pytest.approx(90.0)
    tracker.download("140", {"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
    assert tracker.progress == pytest.approx(95.0)
    tracker.download("140", {"status": "finished", "downloaded_bytes": 10, "total_bytes": 10})
    assert tracker.progress == pytest.approx(100.0)


def test_merged_download_without_sizes_uses_equal_weights():
    tracker = ProgressTracker()
    tracker.plan([{"format_id": "v"}, {"format_id": "a", "filesize": 10}])
    tracker.download("v", {"status": "finished"})
    tracker.download("a", {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
    assert tracker.progress == pytest.approx(75.0)


def test_progress_never_moves_backwards_when_estimate_grows():
    tracker = ProgressTracker()
    tracker.download("hls", {"downloaded_bytes": 50, "total_bytes_estimate": 100})
    tracker.download("hls", {"downloaded_bytes": 55, "total_bytes_estimate": 200})
    assert tracker.progress == 50.0


def test_unknown_total_is_indeterminate():
    tracker = ProgressTracker()
    tracker.download("live", {"downloaded_bytes": 50})
    assert tracker.snapshot()["progress_indeterminate"] == 1


def test_new_phase_restarts_progress():
    tracker = ProgressTracker()
    tracker.download("18", {"status": "finished"})
    tracker.set_phase(CONVERTING, 12.5)
    assert tracker.snapshot() == {
        "phase": CONVERTING, "progress": 12.5, "progress_indeterminate": 0, "speed": None, "eta": None,
    }


def test_ffmpeg_percent():
    assert ffmpeg_percent("out_time_us=30000000\n", 120.0) == 25.0
    assert ffmpeg_percent("out_time_us=N/A", 120.0) is None
    assert ffmpeg_percent("out_time_us=30000000", None) is None
    assert ffmpeg_percent("frame=10", 120.0) is None
