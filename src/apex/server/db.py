"""SQLite storage for roll entries, the name roster, and collected watch files.

Timestamps are assigned here, never by clients, so the server alone decides the
order of rolls. Roll numbers ("Solaris Roll 3") are the rank of a roll among
that buggy's rolls on its local day; they are computed on read, never stored.

Raw FIT bytes are kept content-addressed under `raw/`, so every run can be
re-parsed from the original file later. SQLite holds only a summary of each:
the whole recording (`start_utc`, `duration_sec`, ...) and, separately, the roll
found inside it (`roll_*`), which is what people care about. The watch runs for
minutes either side of the roll, so the two differ a lot.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from apex.analysis import ANALYSIS_VERSION, analyze
from apex.parsing import GarminParser, default_course, detect_roll
from apex.parsing.schema import CourseSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS buggy (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    last_used_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS driver (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    last_used_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pusher (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    last_used_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roll (
    id INTEGER PRIMARY KEY,
    buggy_id INTEGER NOT NULL REFERENCES buggy(id),
    driver_id INTEGER REFERENCES driver(id),
    notes TEXT NOT NULL DEFAULT '',
    created_utc TEXT NOT NULL,
    day TEXT NOT NULL  -- server-local date the roll was created
    -- plus edited_utc and HILL_COLUMNS, added by Store._migrate
);
CREATE TABLE IF NOT EXISTS watch_file (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    buggy TEXT,  -- free-text label from collect-rolls, not a roster entry
    device_id TEXT,
    device_name TEXT,
    -- the whole recording, watch start to watch stop
    start_utc TEXT,
    end_utc TEXT,
    duration_sec REAL,
    total_dist_m REAL,
    max_speed_mph REAL,
    point_count INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    uploaded_utc TEXT NOT NULL,
    day TEXT NOT NULL,  -- server-local date of the recording (upload, if unknown)
    roll_id INTEGER REFERENCES roll(id),
    assigned_utc TEXT
    -- plus ROLL_COLUMNS, added by Store._migrate
);
CREATE INDEX IF NOT EXISTS roll_day ON roll(day);
CREATE INDEX IF NOT EXISTS watch_file_day ON watch_file(day);
CREATE INDEX IF NOT EXISTS watch_file_roll ON watch_file(roll_id);
-- Derived from the raw file by apex.analysis; recomputed when its version changes.
CREATE TABLE IF NOT EXISTS file_analysis (
    file_id INTEGER PRIMARY KEY REFERENCES watch_file(id),
    version INTEGER NOT NULL,
    summary TEXT,  -- JSON; NULL when the recording holds no roll
    detail TEXT    -- JSON: summary plus trace and gate crossings
);
"""

# Every roll with the time it happened. Once data is assigned, that is the
# detected roll start in its file (the earliest, should an older database hold
# several for one roll): the watch saw the buggy cross the
# start gate, which beats the moment someone pressed Create. Until then, it is the
# creation time. The roll's day, and so its number, follow the same time. (With
# MIN(), SQLite takes the bare `day` column from the row holding the minimum.)
# Who pushed each hill: optional, one pusher per hill, from a shared roster.
HILLS = 5
HILL_COLUMNS = [f"hill{n}_pusher_id" for n in range(1, HILLS + 1)]

ROLLS_CTE = f"""
data_time AS (
    SELECT roll_id, MIN(roll_start_utc) AS data_start_utc, day AS data_day
    FROM watch_file
    WHERE roll_id IS NOT NULL AND roll_start_utc IS NOT NULL
    GROUP BY roll_id
),
timed AS (
    SELECT r.id, r.buggy_id, r.driver_id, r.notes, r.created_utc, r.edited_utc,
           {", ".join(f"r.{c}" for c in HILL_COLUMNS)},
           d.data_start_utc,
           COALESCE(d.data_start_utc, r.created_utc) AS at_utc,
           COALESCE(d.data_day, r.day) AS day
    FROM roll r LEFT JOIN data_time d ON d.roll_id = r.id
)
"""

ROLL_QUERY = f"""
WITH {ROLLS_CTE},
numbered AS (
    SELECT t.*, ROW_NUMBER() OVER (
        PARTITION BY t.buggy_id, t.day ORDER BY t.at_utc, t.id
    ) AS number
    FROM timed t
)
SELECT n.id, n.notes, n.created_utc, n.edited_utc, n.data_start_utc, n.at_utc,
       n.day, n.number, b.name AS buggy, d.name AS driver,
       {", ".join(f"p{i}.name AS hill{i}_pusher" for i in range(1, HILLS + 1))}
FROM numbered n
JOIN buggy b ON b.id = n.buggy_id
LEFT JOIN driver d ON d.id = n.driver_id
{"".join(f"LEFT JOIN pusher p{i} ON p{i}.id = n.hill{i}_pusher_id " for i in range(1, HILLS + 1))}
"""

# The roll inside the recording, from the start-gate crossing to the last gate
# crossed. All null when no roll was detected.
ROLL_COLUMNS = {
    "roll_start_utc": "TEXT",
    "roll_end_utc": "TEXT",
    "roll_duration_sec": "REAL",
    "roll_dist_m": "REAL",
    "roll_max_speed_mph": "REAL",
    "roll_end_gate": "TEXT",  # name of the last gate crossed
    "roll_finished": "INTEGER",  # 1 if that was the course's final gate
    "roll_analyzed": "INTEGER NOT NULL DEFAULT 0",  # detection has run on this row
}

FILE_COLUMNS = ", ".join(
    [
        "id", "sha256", "filename", "buggy", "device_id", "device_name", "start_utc",
        "end_utc", "duration_sec", "total_dist_m", "max_speed_mph", "point_count",
        "parse_error", "uploaded_utc", "day", "roll_id", "assigned_utc",
        *(c for c in ROLL_COLUMNS if c != "roll_analyzed"),
    ]
)


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def local_day(dt: datetime) -> str:
    """The server-local calendar date of an aware datetime."""
    return dt.astimezone().date().isoformat()


def roll_title(buggy: str, number: int, day: str) -> str:
    return f"{buggy} Roll {number} ({date.fromisoformat(day):%m/%d/%y})"


class Store:
    def __init__(self, data_dir: Path, course: CourseSpec | None = None):
        self.data_dir = data_dir
        self.raw_dir = data_dir / "raw"
        self.db_path = data_dir / "apex.db"
        self.course = course or default_course()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
        self._backfill_rolls()
        self._backfill_analysis()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced since a database was created."""
        added = {
            "watch_file": ROLL_COLUMNS,
            "roll": {"edited_utc": "TEXT"} | {c: "INTEGER REFERENCES pusher(id)" for c in HILL_COLUMNS},
        }
        for table, columns in added.items():
            have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _backfill_rolls(self) -> None:
        """Run roll detection on files stored before it existed, from their raw bytes."""
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT id, sha256 FROM watch_file WHERE roll_analyzed = 0"
            ).fetchall()
        for row in pending:
            path = self.raw_path(row["sha256"])
            summary = self._analyze(path)[0] if path.exists() else {}
            roll = {k: v for k, v in summary.items() if k in ROLL_COLUMNS}
            roll["roll_analyzed"] = 1
            with self._connect() as conn:
                conn.execute(
                    f"UPDATE watch_file SET {', '.join(f'{k} = ?' for k in roll)} WHERE id = ?",
                    [*roll.values(), row["id"]],
                )

    def _backfill_analysis(self) -> None:
        """Analyze files that have no analysis yet, or one from an older version."""
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT w.id, w.sha256 FROM watch_file w "
                "LEFT JOIN file_analysis a ON a.file_id = w.id "
                "WHERE w.parse_error IS NULL AND (a.file_id IS NULL OR a.version != ?)",
                (ANALYSIS_VERSION,),
            ).fetchall()
        for row in pending:
            path = self.raw_path(row["sha256"])
            if path.exists():
                self._save_analysis(row["id"], self._analyze(path)[1])

    def _save_analysis(self, file_id: int, analysis: tuple[dict, dict] | None) -> None:
        summary, detail = analysis if analysis else (None, None)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO file_analysis (file_id, version, summary, detail) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET version = excluded.version, "
                "summary = excluded.summary, detail = excluded.detail",
                (
                    file_id,
                    ANALYSIS_VERSION,
                    json.dumps(summary) if summary else None,
                    json.dumps(detail) if detail else None,
                ),
            )

    def roll_analyses(self) -> dict[int, dict]:
        """{roll id: {"file_id", "summary"}} for every roll whose data holds a roll.

        Should an older database pair several files with one roll, the one whose
        roll started first is used, matching how the roll is timed.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.roll_id, w.id AS file_id, a.summary FROM watch_file w "
                "JOIN file_analysis a ON a.file_id = w.id "
                "WHERE w.roll_id IS NOT NULL AND a.summary IS NOT NULL "
                "ORDER BY w.roll_start_utc DESC"
            ).fetchall()
        # Later rows overwrite earlier ones, so the earliest roll start wins.
        return {r["roll_id"]: {"file_id": r["file_id"], "summary": json.loads(r["summary"])} for r in rows}

    def analysis_detail(self, file_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail FROM file_analysis WHERE file_id = ?", (file_id,)
            ).fetchone()
        return json.loads(row["detail"]) if row and row["detail"] else None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection per operation; commits on success, rolls back on error."""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ── roster ───────────────────────────────────────────────────────────────

    def roster(self) -> dict[str, list[str]]:
        """Buggy, driver and pusher names in use by some roll, most recently used first.

        Only names a roll still uses: a typo corrected by editing drops out.
        """
        uses = {
            "buggy": "roll.buggy_id = t.id",
            "driver": "roll.driver_id = t.id",
            "pusher": "t.id IN (" + ", ".join(f"roll.{c}" for c in HILL_COLUMNS) + ")",
        }
        with self._connect() as conn:
            return {
                table: [
                    row["name"]
                    for row in conn.execute(
                        f"SELECT name FROM {table} t WHERE EXISTS "
                        f"(SELECT 1 FROM roll WHERE {used}) "
                        "ORDER BY last_used_utc DESC, name"
                    )
                ]
                for table, used in uses.items()
            }

    @staticmethod
    def _resolve_name(conn: sqlite3.Connection, table: str, name: str, now: str) -> int:
        """Id of the case-insensitive match for `name`, creating it on first use."""
        conn.execute(
            f"INSERT INTO {table} (name, last_used_utc) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_used_utc = excluded.last_used_utc",
            (name, now),
        )
        return conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()[0]

    @classmethod
    def _resolve_pushers(
        cls, conn: sqlite3.Connection, pushers: list[str | None] | None, now: str
    ) -> list[int | None]:
        """Pusher ids for hills 1..HILLS; missing or blank hills have none."""
        names = list(pushers or [])[:HILLS]
        names += [None] * (HILLS - len(names))
        return [cls._resolve_name(conn, "pusher", n, now) if n else None for n in names]

    # ── rolls ────────────────────────────────────────────────────────────────

    def create_roll(
        self,
        buggy: str,
        driver: str | None = None,
        notes: str = "",
        now: datetime | None = None,
        pushers: list[str | None] | None = None,
    ) -> dict:
        """`pushers` names hill 1's pusher first; hills left out or blank have none."""
        moment = now or _utc_now()
        stamp = _iso(moment)
        with self._connect() as conn:
            buggy_id = self._resolve_name(conn, "buggy", buggy, stamp)
            driver_id = self._resolve_name(conn, "driver", driver, stamp) if driver else None
            pusher_ids = self._resolve_pushers(conn, pushers, stamp)
            roll_id = conn.execute(
                f"INSERT INTO roll (buggy_id, driver_id, notes, created_utc, day, "
                f"{', '.join(HILL_COLUMNS)}) VALUES (?, ?, ?, ?, ?{', ?' * HILLS})",
                (buggy_id, driver_id, notes, stamp, local_day(moment), *pusher_ids),
            ).lastrowid
        return self.get_roll(roll_id)

    def update_roll(
        self,
        roll_id: int,
        buggy: str,
        driver: str | None = None,
        notes: str = "",
        now: datetime | None = None,
        pushers: list[str | None] | None = None,
    ) -> dict:
        """Replace a roll's buggy, driver, pushers and notes. Its timestamp, and so
        its place in the order, is unchanged; its number follows the new buggy."""
        stamp = _iso(now or _utc_now())
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM roll WHERE id = ?", (roll_id,)).fetchone() is None:
                raise NotFound(f"no roll {roll_id}")
            buggy_id = self._resolve_name(conn, "buggy", buggy, stamp)
            driver_id = self._resolve_name(conn, "driver", driver, stamp) if driver else None
            pusher_ids = self._resolve_pushers(conn, pushers, stamp)
            conn.execute(
                "UPDATE roll SET buggy_id = ?, driver_id = ?, notes = ?, edited_utc = ?, "
                + ", ".join(f"{c} = ?" for c in HILL_COLUMNS)
                + " WHERE id = ?",
                (buggy_id, driver_id, notes, stamp, *pusher_ids, roll_id),
            )
        return self.get_roll(roll_id)

    def delete_roll(self, roll_id: int) -> None:
        """Remove a roll; any data assigned to it becomes unassigned again."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE watch_file SET roll_id = NULL, assigned_utc = NULL WHERE roll_id = ?",
                (roll_id,),
            )
            if not conn.execute("DELETE FROM roll WHERE id = ?", (roll_id,)).rowcount:
                raise NotFound(f"no roll {roll_id}")

    def get_roll(self, roll_id: int) -> dict:
        rolls = self.list_rolls(roll_id=roll_id)
        if not rolls:
            raise NotFound(f"no roll {roll_id}")
        return rolls[0]

    def list_rolls(self, day: str | None = None, roll_id: int | None = None) -> list[dict]:
        """Rolls newest first by when they happened, each with its number, title and
        assigned file ids. `at_utc` is that time; `data_start_utc` is set when it
        came from assigned data rather than from `created_utc`."""
        where, params = [], []
        if day is not None:
            where.append("n.day = ?")
            params.append(day)
        if roll_id is not None:
            where.append("n.id = ?")
            params.append(roll_id)
        sql = ROLL_QUERY + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY n.at_utc DESC, n.id DESC"

        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
            ids = [r["id"] for r in rows]
            files: dict[int, list[int]] = {i: [] for i in ids}
            if ids:
                marks = ",".join("?" * len(ids))
                for f in conn.execute(
                    f"SELECT id, roll_id FROM watch_file WHERE roll_id IN ({marks}) "
                    "ORDER BY COALESCE(roll_start_utc, start_utc), id",
                    ids,
                ):
                    files[f["roll_id"]].append(f["id"])

        for r in rows:
            r["pushers"] = [r.pop(f"hill{i}_pusher") for i in range(1, HILLS + 1)]
            r["title"] = roll_title(r["buggy"], r["number"], r["day"])
            r["file_ids"] = files[r["id"]]
        return rows

    # ── watch files ──────────────────────────────────────────────────────────

    def raw_path(self, sha256: str) -> Path:
        return self.raw_dir / sha256[:2] / f"{sha256}.fit"

    def ingest_file(
        self, content: bytes, filename: str, buggy: str | None, now: datetime | None = None
    ) -> tuple[str, dict]:
        """Store a FIT file once, keyed by its content.

        Returns (status, file), status being "created", "exists", or
        "buggy_set" when a file seen before without a buggy label now gets one.
        An existing label is never overwritten.
        """
        sha = hashlib.sha256(content).hexdigest()
        moment = now or _utc_now()

        existing = self._file_by_sha(sha)
        if existing is not None:
            if existing["buggy"] is None and buggy:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE watch_file SET buggy = ? WHERE id = ? AND buggy IS NULL",
                        (buggy, existing["id"]),
                    )
                return "buggy_set", self.get_file(existing["id"])
            return "exists", existing

        path = self.raw_path(sha)
        if not path.exists():
            _atomic_write(path, content)

        summary, analysis = self._analyze(path)
        start = summary.get("roll_start_utc") or summary.get("start_utc")
        day = local_day(datetime.fromisoformat(start)) if start else local_day(moment)

        columns = {
            "sha256": sha,
            "filename": filename,
            "buggy": buggy or None,
            "uploaded_utc": _iso(moment),
            "day": day,
            **summary,
        }
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO watch_file ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))}) ON CONFLICT(sha256) DO NOTHING",
                list(columns.values()),
            )
            inserted = cursor.rowcount == 1
        if not inserted:
            # Lost a race with a concurrent upload of the same bytes.
            return self.ingest_file(content, filename, buggy, now)
        stored = self._file_by_sha(sha)
        if summary["parse_error"] is None:
            self._save_analysis(stored["id"], analysis)
        return "created", stored

    def _analyze(self, path: Path) -> tuple[dict, tuple[dict, dict] | None]:
        """Parse a stored FIT file once: its summary columns (the recording, and
        the roll in it), and its doomscROLL analysis (None if it holds no roll)."""
        summary: dict = {"point_count": 0, "parse_error": None, "roll_analyzed": 1}
        try:
            recording = GarminParser.parse_fit(str(path))
        except Exception as exc:  # a truncated or corrupt file is still kept raw
            summary["parse_error"] = f"{type(exc).__name__}: {exc}"
            return summary, None

        summary.update(
            device_id=recording.device_id,
            device_name=recording.device_name,
            start_utc=_iso(recording.start_time) if recording.start_time else None,
            end_utc=_iso(recording.end_time) if recording.end_time else None,
            duration_sec=recording.duration_sec,
            total_dist_m=recording.total_dist_m,
            max_speed_mph=recording.max_speed_mph,
            point_count=recording.point_count,
        )
        if window := detect_roll(recording, self.course):
            summary.update(
                roll_start_utc=_iso(window.start_time),
                roll_end_utc=_iso(window.end_time),
                roll_duration_sec=window.duration_sec,
                roll_dist_m=window.distance_m,
                roll_max_speed_mph=window.max_speed_mph,
                roll_end_gate=window.end_gate,
                roll_finished=int(window.finished),
            )
        return summary, analyze(recording, self.course)

    def _file_by_sha(self, sha: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {FILE_COLUMNS} FROM watch_file WHERE sha256 = ?", (sha,)
            ).fetchone()
        return dict(row) if row else None

    def get_file(self, file_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {FILE_COLUMNS} FROM watch_file WHERE id = ?", (file_id,)
            ).fetchone()
        if row is None:
            raise NotFound(f"no file {file_id}")
        return dict(row)

    def list_files(self, day: str | None = None) -> list[dict]:
        """Files newest recording first. Always includes files assigned to rolls
        of `day`, so a roll's data is listed even if it was recorded on another day."""
        sql = f"SELECT {FILE_COLUMNS} FROM watch_file"
        params: list = []
        if day is not None:
            sql = (
                f"WITH {ROLLS_CTE} {sql} "
                "WHERE day = ? OR roll_id IN (SELECT id FROM timed WHERE day = ?)"
            )
            params = [day, day]
        sql += " ORDER BY COALESCE(roll_start_utc, start_utc, uploaded_utc) DESC, id DESC"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def assign_file(self, file_id: int, roll_id: int, now: datetime | None = None) -> dict:
        """Pair an unassigned file with a roll that has no data yet.

        One file per roll, one roll per file. Both conditions are checked in the
        same UPDATE, so two phones assigning at once cannot both succeed.
        Changing a pairing means unassigning first.
        """
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM roll WHERE id = ?", (roll_id,)).fetchone() is None:
                raise NotFound(f"no roll {roll_id}")
            updated = conn.execute(
                "UPDATE watch_file SET roll_id = ?, assigned_utc = ? "
                "WHERE id = ? AND roll_id IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM watch_file WHERE roll_id = ?)",
                (roll_id, _iso(now or _utc_now()), file_id, roll_id),
            ).rowcount
        if not updated:
            if self.get_file(file_id)["roll_id"] is not None:  # NotFound if no such file
                raise Conflict(f"file {file_id} is already assigned to a roll")
            raise Conflict(f"roll {roll_id} already has data assigned")
        return self.get_file(file_id)

    def unassign_file(self, file_id: int) -> dict:
        with self._connect() as conn:
            conn.execute(
                "UPDATE watch_file SET roll_id = NULL, assigned_utc = NULL WHERE id = ?",
                (file_id,),
            )
        return self.get_file(file_id)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
