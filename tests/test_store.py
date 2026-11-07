from datetime import UTC, datetime, timedelta

import pytest
from conftest import SAMPLE_FIT

from apex.server.db import Conflict, local_day

T0 = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)


def test_roll_numbers_count_per_buggy_per_day(store):
    a1 = store.create_roll("Solaris", now=T0)
    b1 = store.create_roll("Kingfisher", now=T0 + timedelta(minutes=1))
    a2 = store.create_roll("solaris", now=T0 + timedelta(minutes=2))  # same buggy, any case
    next_day = store.create_roll("Solaris", now=T0 + timedelta(days=1))

    assert [a1["number"], b1["number"], next_day["number"]] == [1, 1, 1]
    assert store.get_roll(a2["id"])["number"] == 2
    assert a2["buggy"] == "Solaris"  # first spelling is kept
    assert a1["title"] == "Solaris Roll 1 (10/31/26)"


def test_list_rolls_filters_by_day_newest_first(store):
    store.create_roll("Solaris", now=T0 - timedelta(days=1))
    first = store.create_roll("Solaris", now=T0)
    second = store.create_roll("Solaris", driver="Mia", notes="wet", now=T0 + timedelta(minutes=5))

    rolls = store.list_rolls(day=local_day(T0))
    assert [r["id"] for r in rolls] == [second["id"], first["id"]]
    assert rolls[0]["driver"] == "Mia" and rolls[0]["notes"] == "wet"
    assert len(store.list_rolls()) == 3


def test_roster_is_most_recent_first(store):
    store.create_roll("Solaris", driver="Mia", now=T0)
    store.create_roll("Kingfisher", driver="Ana", now=T0 + timedelta(minutes=1))
    store.create_roll("Solaris", now=T0 + timedelta(minutes=2))
    store.create_roll("Solaris", pushers=[None, "Bo"], now=T0 + timedelta(minutes=3))
    assert store.roster() == {
        "buggy": ["Solaris", "Kingfisher"],
        "driver": ["Ana", "Mia"],
        "pusher": ["Bo"],
    }


def test_pushers_added_to_an_older_database(tmp_path):
    import sqlite3

    from apex.server.db import Store

    # A database from before pushers existed: no pusher table or hill columns.
    with sqlite3.connect(tmp_path / "apex.db") as conn:
        conn.executescript(
            "CREATE TABLE buggy (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, last_used_utc TEXT NOT NULL);"
            "CREATE TABLE driver (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, last_used_utc TEXT NOT NULL);"
            "CREATE TABLE roll (id INTEGER PRIMARY KEY, buggy_id INTEGER NOT NULL, driver_id INTEGER, notes TEXT NOT NULL DEFAULT '', created_utc TEXT NOT NULL, day TEXT NOT NULL);"
            "INSERT INTO buggy VALUES (1, 'Solaris', '2026-10-31T14:00:00.000+00:00');"
            "INSERT INTO roll VALUES (1, 1, NULL, '', '2026-10-31T14:00:00.000+00:00', '2026-10-31');"
        )
    store = Store(tmp_path)
    assert store.get_roll(1)["pushers"] == [None] * 5
    edited = store.update_roll(1, "Solaris", pushers=["Ana"])
    assert edited["pushers"] == ["Ana", None, None, None, None]


def test_ingest_parses_and_keeps_raw_bytes(store):
    content = SAMPLE_FIT.read_bytes()
    status, f = store.ingest_file(content, "GA373120.FIT", "Solaris")

    assert status == "created"
    assert f["point_count"] == 710 and f["parse_error"] is None
    assert f["start_utc"].startswith("2026-10-03T11:31:20")
    assert f["roll_start_utc"].startswith("2026-10-03T11:38:53")
    assert f["roll_finished"] == 1 and 60 < f["roll_duration_sec"] < 70
    assert f["day"] == local_day(datetime.fromisoformat(f["start_utc"]))
    assert store.raw_path(f["sha256"]).read_bytes() == content


def test_ingest_never_overwrites_a_buggy_label(store):
    content = SAMPLE_FIT.read_bytes()
    store.ingest_file(content, "GA373120.FIT", "Solaris")

    status, f = store.ingest_file(content, "renamed.FIT", "Kingfisher")
    assert status == "exists"
    assert f["buggy"] == "Solaris" and f["filename"] == "GA373120.FIT"


def test_ingest_fills_in_a_missing_buggy_label(store):
    content = SAMPLE_FIT.read_bytes()
    assert store.ingest_file(content, "a.FIT", None)[0] == "created"
    assert store.ingest_file(content, "a.FIT", None)[0] == "exists"

    status, f = store.ingest_file(content, "a.FIT", "Solaris")
    assert status == "buggy_set" and f["buggy"] == "Solaris"


def test_unparseable_file_is_kept(store):
    status, f = store.ingest_file(b"not a fit file", "junk.FIT", None, now=T0)
    assert status == "created"
    assert f["parse_error"] and f["point_count"] == 0
    assert f["day"] == local_day(T0)
    assert store.raw_path(f["sha256"]).exists()


def test_assign_only_unassigned(store):
    roll = store.create_roll("Solaris")
    other = store.create_roll("Kingfisher")
    _, f = store.ingest_file(SAMPLE_FIT.read_bytes(), "a.FIT", "Solaris")

    assert store.assign_file(f["id"], roll["id"])["roll_id"] == roll["id"]
    with pytest.raises(Conflict):
        store.assign_file(f["id"], other["id"])
    assert store.get_roll(roll["id"])["file_ids"] == [f["id"]]

    store.unassign_file(f["id"])
    assert store.assign_file(f["id"], other["id"])["roll_id"] == other["id"]


def test_assigned_data_sets_the_rolls_time_day_and_number(store):
    """The roll start in the data beats the entry's creation time."""
    _, f = store.ingest_file(SAMPLE_FIT.read_bytes(), "a.FIT", None)  # roll at 11:38:53 UTC
    sample_time = datetime.fromisoformat(f["roll_start_utc"])
    logged_late = store.create_roll("Solaris", now=sample_time + timedelta(minutes=20))
    logged_first = store.create_roll("Solaris", now=sample_time + timedelta(minutes=10))
    assert store.get_roll(logged_first["id"])["number"] == 1

    store.assign_file(f["id"], logged_late["id"])
    late = store.get_roll(logged_late["id"])
    assert late["at_utc"] == late["data_start_utc"] == f["roll_start_utc"]
    assert late["created_utc"] == logged_late["created_utc"]
    # Its data shows it rolled before the other entry was logged: it is now Roll 1.
    assert late["number"] == 1 and store.get_roll(logged_first["id"])["number"] == 2
    assert [r["id"] for r in store.list_rolls(day=f["day"])] == [logged_first["id"], late["id"]]


def test_data_without_a_detected_roll_leaves_the_logged_time(store):
    roll = store.create_roll("Solaris", now=T0)
    _, junk = store.ingest_file(b"not a fit file", "junk.FIT", None, now=T0)
    store.assign_file(junk["id"], roll["id"])
    r = store.get_roll(roll["id"])
    assert r["data_start_utc"] is None and r["at_utc"] == r["created_utc"]


def test_files_listed_with_the_day_of_their_roll(store):
    """A file shows under its roll's day, even if the file was recorded on another."""
    roll = store.create_roll("Solaris", now=T0)
    _, junk = store.ingest_file(b"junk", "j.FIT", None, now=T0 - timedelta(days=3))
    assert store.list_files(day=local_day(T0)) == []
    store.assign_file(junk["id"], roll["id"])
    assert [x["id"] for x in store.list_files(day=local_day(T0))] == [junk["id"]]


def test_a_roll_takes_only_one_file(store):
    roll = store.create_roll("Solaris")
    _, first = store.ingest_file(SAMPLE_FIT.read_bytes(), "a.FIT", None)
    _, second = store.ingest_file(b"another recording", "b.FIT", None)
    store.assign_file(first["id"], roll["id"])

    with pytest.raises(Conflict, match="already has data"):
        store.assign_file(second["id"], roll["id"])
    assert store.get_file(second["id"])["roll_id"] is None

    store.unassign_file(first["id"])
    assert store.assign_file(second["id"], roll["id"])["roll_id"] == roll["id"]
