"""Find watch recordings on a mounted Garmin and send them to the server.

Only each file's `file_id` header is read here (well under a millisecond), which
is enough to skip non-activity files (a Garmin mount also holds monitoring and
settings FITs) and recordings from other days. The raw bytes are uploaded and
the server does the full parse, so nothing is lost for later reconstruction.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import fitparse

Uploader = Callable[[Path, str | None], dict]


class CollectError(RuntimeError):
    """A failure that stops collection outright, such as an unreachable server."""


@dataclass
class WatchFile:
    path: Path
    kind: str | None  # file_id.type, e.g. "activity"; None if the header is unreadable
    created: datetime  # recording start, falling back to the file's mtime

    @property
    def local_day(self) -> date:
        return self.created.astimezone().date()


def find_fit_files(root: Path) -> list[Path]:
    """`root` itself if it is a file, else every *.fit beneath it (any case)."""
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.suffix.lower() == ".fit" and p.is_file())


def read_header(path: Path) -> WatchFile:
    kind, created = None, None
    try:
        for msg in fitparse.FitFile(str(path)).get_messages("file_id"):
            values = msg.get_values()
            kind = values.get("type")
            if isinstance(ts := values.get("time_created"), datetime):
                created = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            break
    except fitparse.FitParseError:
        pass  # still worth sending: the server keeps the raw bytes either way
    if created is None:
        created = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return WatchFile(path=path, kind=str(kind) if kind is not None else None, created=created)


def select_files(
    paths: Iterable[Path], *, include_all: bool, today: date | None = None
) -> tuple[list[WatchFile], int, int]:
    """Activity files to send, oldest first, plus counts of (other-day, non-activity) skips."""
    today = today or datetime.now().astimezone().date()
    chosen, other_days, not_activity = [], 0, 0
    for path in paths:
        wf = read_header(path)
        if wf.kind is not None and wf.kind != "activity":
            not_activity += 1
        elif not include_all and wf.local_day != today:
            other_days += 1
        else:
            chosen.append(wf)
    chosen.sort(key=lambda wf: wf.created)
    return chosen, other_days, not_activity


def _request(request: urllib.request.Request, timeout: float) -> dict:
    """Send a request to the server and decode its JSON reply."""
    return json.loads(_fetch(request, timeout))


def _fetch(request: urllib.request.Request, timeout: float) -> bytes:
    """Send a request to the server, turning failures into readable errors."""
    base = f"{urllib.parse.urlsplit(request.full_url).scheme}://{request.host}"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 401:
            raise CollectError(
                f"{base} rejected the password ({detail}). "
                "Update it with `apex config set password`."
            ) from exc
        if exc.code == 429:
            raise CollectError(f"{base} is refusing attempts for now: {detail}") from exc
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CollectError(f"can't reach the server at {base} ({reason})") from exc


def ping(server: str, secret: str, timeout: float = 5.0) -> None:
    """Check the server is reachable and accepts `secret`; raises CollectError if not."""
    request = urllib.request.Request(
        f"{server.rstrip('/')}/api/ping", headers={"Authorization": f"Bearer {secret}"}
    )
    _request(request, timeout)


def http_uploader(server: str, secret: str, timeout: float = 30.0) -> Uploader:
    """POST raw file bytes to `server`, authenticating with a bearer secret."""
    base = server.rstrip("/")

    def upload(path: Path, buggy: str | None) -> dict:
        query = {"filename": path.name}
        if buggy:
            query["buggy"] = buggy
        request = urllib.request.Request(
            f"{base}/api/files?{urllib.parse.urlencode(query)}",
            data=path.read_bytes(),
            method="POST",
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/octet-stream",
            },
        )
        return _request(request, timeout)

    return upload


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.load(exc).get("detail", exc.reason))
    except (json.JSONDecodeError, AttributeError):
        return str(exc.reason)


@dataclass
class Outcome:
    file: WatchFile
    status: str  # "created" | "exists" | "buggy_set" | "failed"
    stored: dict | None = None  # the server's record of the file
    error: str | None = None


@dataclass
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    other_days: int = 0
    not_activity: int = 0

    def count(self, *statuses: str) -> int:
        return sum(o.status in statuses for o in self.outcomes)


def collect(
    root: Path,
    *,
    buggy: str | None,
    include_all: bool,
    upload: Uploader,
    on_outcome: Callable[[Outcome], None] = lambda o: None,
    today: date | None = None,
) -> Report:
    """Send matching files under `root`, reporting each result as it arrives.

    A failed file is recorded and collection continues; a `CollectError` (no
    files, server unreachable, bad credentials) stops it.
    """
    paths = find_fit_files(root)
    if not paths:
        raise CollectError(f"no .fit files found under {root}")

    chosen, other_days, not_activity = select_files(paths, include_all=include_all, today=today)
    report = Report(other_days=other_days, not_activity=not_activity)

    for wf in chosen:
        try:
            result = upload(wf.path, buggy)
        except CollectError:
            raise
        except Exception as exc:  # one bad file must not stop the rest
            outcome = Outcome(wf, "failed", error=str(exc))
        else:
            outcome = Outcome(wf, result["status"], stored=result["file"])
        report.outcomes.append(outcome)
        on_outcome(outcome)
    return report
