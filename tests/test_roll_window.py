import sqlite3

import pytest

from apex.parsing import GarminParser, default_course, detect_roll
from apex.server.db import Store
from conftest import SAMPLE_FIT


@pytest.fixture(scope="module")
def recording():
    return GarminParser.parse_fit(str(SAMPLE_FIT))


def test_roll_is_the_gate_to_gate_span_not_the_recording(recording):
    window = detect_roll(recording, default_course())
    assert window is not None and window.finished
    assert window.end_gate == "Hill 3 Pickup"
    # The watch ran 11:49 from 11:31:20; the roll is ~67 s starting 11:38:53.
    assert recording.duration_sec == 709
    assert window.start_time.isoformat().startswith("2026-10-03T11:38:53")
    assert 60 < window.duration_sec < 70
    assert window.duration_sec == pytest.approx(
        (window.end_time - window.start_time).total_seconds()
    )
    assert 700 < window.distance_m < 850
    assert window.max_speed_mph <= recording.max_speed_mph


def test_no_roll_before_the_start_gate(recording):
    before = recording.model_copy(update={"records": recording.records[:400]})
    assert detect_roll(before, default_course()) is None


def test_partial_roll_ends_at_last_gate_crossed(recording):
    course = default_course()
    first = course.segments[0]
    cut = next(i for i, p in enumerate(recording.records) if p.elapsed_sec > 495)
    partial = recording.model_copy(update={"records": recording.records[:cut]})
    window = detect_roll(partial, course)
    assert window is not None and not window.finished
    assert window.end_gate == first.end_gate.name


def test_old_databases_are_migrated_and_backfilled(tmp_path):
    store = Store(tmp_path)
    _, f = store.ingest_file(SAMPLE_FIT.read_bytes(), "a.FIT", None)
    roll_start = f["roll_start_utc"]
    assert roll_start

    # Rebuild the table as it was before roll columns existed.
    with sqlite3.connect(tmp_path / "apex.db") as conn:
        conn.execute("CREATE TABLE old AS SELECT id, sha256, filename, buggy, device_id, "
                     "device_name, start_utc, end_utc, duration_sec, total_dist_m, "
                     "max_speed_mph, point_count, parse_error, uploaded_utc, day, roll_id, "
                     "assigned_utc FROM watch_file")
        conn.execute("DROP TABLE watch_file")
        conn.execute("ALTER TABLE old RENAME TO watch_file")

    reopened = Store(tmp_path)
    assert reopened.get_file(f["id"])["roll_start_utc"] == roll_start
