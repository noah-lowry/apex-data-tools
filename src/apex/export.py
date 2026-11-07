"""Download a day's raw watch files from the server into a folder.

The bytes are exactly what `collect-rolls` uploaded; each download is checked
against the server's SHA-256 before it is written. Exporting again into the same
folder skips files already there, so a re-run only fetches what is new.
"""

import hashlib
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from apex.collect import CollectError, _fetch, _request


@dataclass
class ServerFiles:
    """How export reaches the server: list a day's files, and fetch one's bytes."""

    list_day: Callable[[str], list[dict]]
    download: Callable[[int], bytes]


def http_files(server: str, secret: str, timeout: float = 30.0) -> ServerFiles:
    base = server.rstrip("/")
    headers = {"Authorization": f"Bearer {secret}"}

    def list_day(day: str) -> list[dict]:
        query = urllib.parse.urlencode({"day": day})
        request = urllib.request.Request(f"{base}/api/files?{query}", headers=headers)
        return _request(request, timeout)["files"]

    def download(file_id: int) -> bytes:
        request = urllib.request.Request(f"{base}/api/files/{file_id}/raw", headers=headers)
        return _fetch(request, timeout)

    return ServerFiles(list_day, download)


@dataclass
class Exported:
    file: dict  # the server's record of the file
    status: str  # "saved" | "exists" | "failed"
    path: Path | None = None
    error: str | None = None


@dataclass
class ExportReport:
    dest: Path
    outcomes: list[Exported] = field(default_factory=list)

    def count(self, *statuses: str) -> int:
        return sum(o.status in statuses for o in self.outcomes)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def target_path(dest: Path, file: dict) -> tuple[Path, bool]:
    """Where `file` goes in `dest`, and whether that path already holds it.

    Files keep their watch filename. Should a different file already have that
    name (two watches can number recordings alike), the server's id is appended.
    """
    name = Path(file["filename"]).name or f"{file['id']}.fit"
    path = dest / name
    held = _sha256(path)
    if held is None or held == file["sha256"]:
        return path, held is not None
    path = path.with_stem(f"{path.stem}-{file['id']}")
    return path, _sha256(path) == file["sha256"]


def export_day(
    dest: Path,
    *,
    day: str,
    files: ServerFiles,
    on_outcome: Callable[[Exported], None] = lambda o: None,
) -> ExportReport:
    """Save every file the server holds for `day` into `dest`, oldest first.

    A failed file is recorded and the rest continue; a `CollectError` (server
    unreachable, bad credentials) stops the export.
    """
    listed = files.list_day(day)
    listed.sort(key=lambda f: (f["roll_start_utc"] or f["start_utc"] or f["uploaded_utc"], f["id"]))
    report = ExportReport(dest=dest)
    if listed:
        dest.mkdir(parents=True, exist_ok=True)

    for file in listed:
        path, already = target_path(dest, file)
        if already:
            outcome = Exported(file, "exists", path)
        else:
            try:
                content = files.download(file["id"])
                if hashlib.sha256(content).hexdigest() != file["sha256"]:
                    raise ValueError("download doesn't match the server's checksum")
                _write_new(path, content)
            except CollectError:
                raise
            except Exception as exc:  # one bad file must not stop the rest
                outcome = Exported(file, "failed", error=str(exc))
            else:
                outcome = Exported(file, "saved", path)
        report.outcomes.append(outcome)
        on_outcome(outcome)
    return report


def _write_new(path: Path, content: bytes) -> None:
    """Write via a temporary name, so an interrupted export leaves no partial .fit."""
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(content)
    tmp.replace(path)
