#!/usr/bin/env python3
"""Update the pinned yt-dlp and Deno versions used by the project."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
DENO_RELEASES_URL = "https://api.github.com/repos/denoland/deno/releases/latest"
REQUEST_TIMEOUT_SECONDS = 30

DENO_VERSION_RE = re.compile(r"^(?P<prefix>ARG DENO_VERSION=)(?P<value>[^\r\n]+)$", re.MULTILINE)
DENO_ARCH_RE = re.compile(r"^(?P<prefix>ARG DENO_ARCH=)(?P<value>[^\r\n]+)$", re.MULTILINE)
DENO_SHA256_RE = re.compile(r"^(?P<prefix>ARG DENO_SHA256=)(?P<value>[^\r\n]+)$", re.MULTILINE)
YT_DLP_RE = re.compile(r"(?P<prefix>yt-dlp\[default\]==)(?P<value>[^\s\"]+)(?P<suffix>\")")
CHART_IMAGE_TAG_RE = re.compile(r"^(?P<prefix>  tag:\s+)(?P<value>[^\s#]+)(?P<suffix>[^\r\n]*)$", re.MULTILINE)
README_IMAGE_TAG_RE = re.compile(r"(?P<prefix>--set image\.tag=)(?P<value>[^\s`]+)")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9+_.-]*)?$")
DENO_VERSION_VALUE_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class UpdateError(RuntimeError):
    """Raised when remote metadata or local project files are invalid."""


@dataclass(frozen=True)
class CurrentVersions:
    yt_dlp: str
    deno: str
    deno_arch: str
    deno_sha256: str


@dataclass(frozen=True)
class LatestVersions:
    yt_dlp: str
    deno: str
    deno_sha256: str


def _fetch(url: str, *, accept: str) -> str:
    headers = {
        "Accept": accept,
        "User-Agent": "jelly-downloader-dependency-updater",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        with urlopen(Request(url, headers=headers), timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as error:
        raise UpdateError(f"could not fetch {url}: {error}") from error


def _fetch_json(url: str) -> dict[str, object]:
    try:
        payload = json.loads(_fetch(url, accept="application/json"))
    except json.JSONDecodeError as error:
        raise UpdateError(f"invalid JSON returned by {url}: {error}") from error
    if not isinstance(payload, dict):
        raise UpdateError(f"expected an object from {url}")
    return payload


def _extract_value(content: str, pattern: re.Pattern[str], label: str) -> str:
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        raise UpdateError(f"expected exactly one {label}, found {len(matches)}")
    return matches[0].group("value")


def _validate_version(version: str, label: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise UpdateError(f"{label} has an unexpected version: {version!r}")
    return version


def _replace_value(
    content: str,
    pattern: re.Pattern[str],
    expected: str,
    replacement: str,
    label: str,
) -> str:
    matches = list(pattern.finditer(content))
    if len(matches) != 1:
        raise UpdateError(f"expected exactly one {label}, found {len(matches)}")
    match = matches[0]
    actual = match.group("value")
    if actual != expected:
        raise UpdateError(f"{label} is inconsistent: expected {expected!r}, found {actual!r}")
    return f"{content[:match.start('value')]}{replacement}{content[match.end('value'):]}"


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError(f"could not read {path}: {error}") from error


def read_current_versions(root: Path) -> CurrentVersions:
    dockerfile = _read_file(root / "Dockerfile")
    chart_values = _read_file(root / "chart/video-downloader/values.yaml")
    chart_readme = _read_file(root / "chart/video-downloader/README.md")

    yt_dlp = _validate_version(_extract_value(dockerfile, YT_DLP_RE, "yt-dlp version"), "yt-dlp")
    chart_tag = _extract_value(chart_values, CHART_IMAGE_TAG_RE, "Helm image tag")
    readme_tag = _extract_value(chart_readme, README_IMAGE_TAG_RE, "README image tag")
    if chart_tag != yt_dlp or readme_tag != yt_dlp:
        raise UpdateError(
            "yt-dlp version is out of sync between Dockerfile, Helm values, and chart README"
        )

    deno = _extract_value(dockerfile, DENO_VERSION_RE, "Deno version")
    if not DENO_VERSION_VALUE_RE.fullmatch(deno):
        raise UpdateError(f"Deno has an unexpected version: {deno!r}")
    deno_arch = _extract_value(dockerfile, DENO_ARCH_RE, "Deno architecture")
    deno_sha256 = _extract_value(dockerfile, DENO_SHA256_RE, "Deno SHA-256")
    if not SHA256_RE.fullmatch(deno_sha256):
        raise UpdateError(f"Deno SHA-256 has an unexpected value: {deno_sha256!r}")

    return CurrentVersions(yt_dlp=yt_dlp, deno=deno, deno_arch=deno_arch, deno_sha256=deno_sha256)


def _checksum_for_archive(checksum_text: str, archive_name: str) -> str:
    matching_checksums: list[str] = []
    for line in checksum_text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1].lstrip("*") == archive_name:
            matching_checksums.append(fields[0])

    if not matching_checksums:
        # A checksum file normally names its archive. Supporting a single bare
        # hash also keeps this compatible with a future Deno release format.
        bare_hashes = re.findall(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", checksum_text)
        if len(bare_hashes) == 1:
            matching_checksums = bare_hashes

    if len(matching_checksums) != 1 or not SHA256_RE.fullmatch(matching_checksums[0]):
        raise UpdateError(f"could not find one valid SHA-256 checksum for {archive_name}")
    return matching_checksums[0].lower()


def fetch_latest_versions(deno_arch: str) -> LatestVersions:
    pypi = _fetch_json(PYPI_URL)
    info = pypi.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("version"), str):
        raise UpdateError("PyPI response did not contain a string yt-dlp info.version")
    yt_dlp = info["version"]
    _validate_version(yt_dlp, "latest yt-dlp")

    release = _fetch_json(DENO_RELEASES_URL)
    tag_name = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(tag_name, str) or assets is None:
        raise UpdateError("Deno release response is missing tag_name or assets")
    if not isinstance(assets, list):
        raise UpdateError("Deno release response has an invalid assets list")

    if not tag_name.startswith("v"):
        raise UpdateError(f"Deno release tag has an unexpected format: {tag_name!r}")
    deno = tag_name[1:]
    if not DENO_VERSION_VALUE_RE.fullmatch(deno):
        raise UpdateError(f"latest Deno release has an unexpected version: {deno!r}")

    archive_name = f"deno-{deno_arch}.zip"
    checksum_name = f"{archive_name}.sha256sum"
    checksum_url: str | None = None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != checksum_name:
            continue
        candidate_url = asset.get("browser_download_url")
        if isinstance(candidate_url, str):
            checksum_url = candidate_url
        break
    if checksum_url is None:
        raise UpdateError(f"Deno release did not contain {checksum_name}")

    checksum_text = _fetch(checksum_url, accept="text/plain")
    deno_sha256 = _checksum_for_archive(checksum_text, archive_name)
    return LatestVersions(yt_dlp=yt_dlp, deno=deno, deno_sha256=deno_sha256)


def build_changes(root: Path, current: CurrentVersions, latest: LatestVersions) -> dict[Path, tuple[str, str]]:
    dockerfile_path = root / "Dockerfile"
    values_path = root / "chart/video-downloader/values.yaml"
    readme_path = root / "chart/video-downloader/README.md"
    dockerfile = _read_file(dockerfile_path)
    values = _read_file(values_path)
    readme = _read_file(readme_path)

    updated_dockerfile = _replace_value(
        dockerfile, DENO_VERSION_RE, current.deno, latest.deno, "Deno version"
    )
    updated_dockerfile = _replace_value(
        updated_dockerfile,
        DENO_SHA256_RE,
        current.deno_sha256,
        latest.deno_sha256,
        "Deno SHA-256",
    )
    updated_dockerfile = _replace_value(
        updated_dockerfile, YT_DLP_RE, current.yt_dlp, latest.yt_dlp, "yt-dlp version"
    )
    updated_values = _replace_value(
        values, CHART_IMAGE_TAG_RE, current.yt_dlp, latest.yt_dlp, "Helm image tag"
    )
    updated_readme = _replace_value(
        readme, README_IMAGE_TAG_RE, current.yt_dlp, latest.yt_dlp, "README image tag"
    )
    return {
        dockerfile_path: (dockerfile, updated_dockerfile),
        values_path: (values, updated_values),
        readme_path: (readme, updated_readme),
    }


def _write_text_atomically(path: Path, content: str) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary_file:
            temporary_file.write(content)
            temporary_path = temporary_file.name
        os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise UpdateError(f"could not update {path}: {error}") from error


def _print_diff(root: Path, changes: dict[Path, tuple[str, str]]) -> None:
    for path, (old, new) in changes.items():
        if old == new:
            continue
        relative_path = path.relative_to(root).as_posix()
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
        sys.stdout.writelines(diff)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update the pinned yt-dlp and Deno versions in this repository."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (default: inferred from this script)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the patch without writing files")
    mode.add_argument(
        "--check",
        action="store_true",
        help="check for updates without writing files; exit 1 when updates are available",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()

    try:
        current = read_current_versions(root)
        latest = fetch_latest_versions(current.deno_arch)
        changes = build_changes(root, current, latest)
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    changed_paths = [path for path, (old, new) in changes.items() if old != new]
    if not changed_paths:
        print(f"yt-dlp {current.yt_dlp} and Deno {current.deno} are already up to date.")
        return 0

    if args.dry_run or args.check:
        _print_diff(root, changes)
        if args.check:
            print("Updates are available.", file=sys.stderr)
            return 1
        return 0

    try:
        for path, (_, new) in changes.items():
            if path in changed_paths:
                _write_text_atomically(path, new)
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Updated yt-dlp {current.yt_dlp} -> {latest.yt_dlp}.")
    print(f"Updated Deno {current.deno} -> {latest.deno} ({latest.deno_sha256}).")
    print("Review the changes, run the tests, and build the image before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
