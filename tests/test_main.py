import ipaddress

import pytest

from app.main import is_public_ip, validate_public_url


def test_public_ip_check_rejects_private_and_loopback():
    assert not is_public_ip("127.0.0.1")
    assert not is_public_ip("10.0.0.1")
    assert not is_public_ip("::1")


def test_public_ip_check_accepts_public_ip():
    assert is_public_ip("8.8.8.8")


def test_url_scheme_is_checked():
    with pytest.raises(ValueError, match="HTTP"):
        validate_public_url("file:///tmp/video")


class FakeProcess:
    def __init__(self, lines, return_code=0):
        self.stdout = iter(lines)
        self.return_code = return_code

    def wait(self):
        return self.return_code


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "DOWNLOAD_DIR", tmp_path / "videos")
    monkeypatch.setattr(main, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "state" / "downloader.db")
    monkeypatch.setattr(main, "PROGRESS_WRITE_INTERVAL_SECONDS", 0)
    main.ensure_directories()
    main.initialize_database()
    return main


def test_run_download_records_intermediate_progress(isolated_app, monkeypatch):
    main = isolated_app
    output = main.DOWNLOAD_DIR / "Clip [abc].mp4"
    output.write_bytes(b"video")
    with main.database() as connection:
        connection.execute(
            "INSERT INTO downloads (id, url, status, created_at) VALUES ('job', 'https://example.com/v', 'running', ?)",
            (main.now(),),
        )
    lines = [
        "WARNING: something harmless\n",
        'PLAN\t[{"format_id": "137", "filesize": 90}, {"format_id": "140", "filesize": 10}]\n',
        'TITLE\t"Clip"\n',
        'PROGRESS\t"137"\t{"status": "downloading", "downloaded_bytes": 45, "total_bytes": 90, "speed": 1024, "eta": 3}\n',
        'PROGRESS\t"137"\t{"status": "finished", "downloaded_bytes": 90, "total_bytes": 90}\n',
        'PROGRESS\t"140"\t{"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10}\n',
        'PROGRESS\t"140"\t{"status": "finished", "downloaded_bytes": 10, "total_bytes": 10}\n',
        f"FILEPATH\t{output}\n",
    ]
    monkeypatch.setattr(main.subprocess, "Popen", lambda *args, **kwargs: FakeProcess(lines))
    recorded = []
    original_update = main.update_job

    def spy(job_id, **fields):
        recorded.append(fields)
        original_update(job_id, **fields)

    monkeypatch.setattr(main, "update_job", spy)

    def convert(path, on_progress=None):
        on_progress(50.0)
        return path

    monkeypatch.setattr(main, "ensure_jellyfin_compatibility", convert)

    main.run_download({"id": "job", "url": "https://example.com/v"})

    downloading = [f["progress"] for f in recorded if f.get("phase") == "downloading"]
    assert [p for p in downloading if 0 < p < 100] == [45.0, 90.0, 95.0]
    assert {"phase": "converting", "progress": 50.0} .items() <= next(f for f in recorded if f.get("phase") == "converting").items()
    job = main.get_job("job")
    assert job["status"] == "completed"
    assert job["progress"] == 100.0
    assert job["title"] == "Clip"
    assert job["phase"] is None


def test_initialize_database_migrates_old_schema(tmp_path, monkeypatch):
    import sqlite3

    from app import main

    database_path = tmp_path / "downloader.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE downloads (id TEXT PRIMARY KEY, url TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,"
        " title TEXT, output_path TEXT, error TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT)"
    )
    connection.execute("INSERT INTO downloads (id, url, status, progress, created_at) VALUES ('old', 'u', 'running', 40, 'now')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(main, "DATABASE_PATH", database_path)

    main.initialize_database()
    main.initialize_database()  # Idempotent.

    job = main.get_job("old")
    assert job["status"] == "queued"
    assert job["progress"] == 0
    assert job["phase"] is None and job["progress_indeterminate"] == 0
