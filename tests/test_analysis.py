import sqlite3
from itertools import pairwise

import pytest
from conftest import SAMPLE_FIT

from apex.analysis import ANALYSIS_VERSION, analyze
from apex.parsing import GarminParser, default_course
from apex.server.db import Store


@pytest.fixture(scope="module")
def recording():
    return GarminParser.parse_fit(str(SAMPLE_FIT))


def test_full_roll(recording):
    summary, detail = analyze(recording, default_course())
    assert summary["complete"] and summary["finished"]
    segs = summary["segments"]
    assert [s["status"] for s in segs] == ["ok"] * 4
    # Segments tile the roll: their times sum to the total, end to end.
    assert sum(s["time_sec"] for s in segs) == pytest.approx(summary["total_sec"], abs=0.01)
    assert all(a["t_end"] == b["t_start"] for a, b in pairwise(segs))
    assert segs[0]["t_start"] == 0 and segs[-1]["t_end"] == pytest.approx(summary["total_sec"], abs=0.01)
    assert summary["hill3_mph"] == detail["gates"][-1]["mph"]
    assert [g["name"] for g in detail["gates"]] == [
        "End of Hill 2", "Stop Sign", "Start of Chute Entry", "Chute Exit", "Hill 3 Pickup",
    ]

    trace = detail["trace"]
    assert trace["t"][0] == 0 and trace["d"][0] == 0
    assert trace["t"] == sorted(trace["t"]) and trace["d"] == sorted(trace["d"])
    assert len(summary["spark"]) == len(trace["t"]) == summary["samples"] + 2
    assert "trace" not in summary


def test_partial_roll_marks_unmeasured_segments(recording):
    cut = next(i for i, p in enumerate(recording.records) if p.elapsed_sec > 495)
    summary, detail = analyze(recording.model_copy(update={"records": recording.records[:cut]}), default_course())
    assert not summary["finished"] and not summary["complete"]
    assert summary["hill3_mph"] is None and summary["end_gate"] == "Stop Sign"
    assert [s["status"] for s in summary["segments"]] == ["ok", "missed", "missed", "missed"]
    assert summary["segments"][1] == {"id": "monument_straight", "status": "missed"}
    assert detail["trace"]["d"][-1] == summary["segments"][0]["d_end"]


def test_no_roll(recording):
    assert analyze(recording.model_copy(update={"records": recording.records[:400]}), default_course()) is None


def test_stored_at_ingest_and_recomputed_when_stale(tmp_path):
    store = Store(tmp_path)
    _, f = store.ingest_file(SAMPLE_FIT.read_bytes(), "a.FIT", None)
    _, junk = store.ingest_file(b"not a fit file", "junk.FIT", None)
    detail = store.analysis_detail(f["id"])
    assert detail["complete"] and detail["version"] == ANALYSIS_VERSION
    assert store.analysis_detail(junk["id"]) is None

    with sqlite3.connect(tmp_path / "apex.db") as conn:
        conn.execute("UPDATE file_analysis SET version = 0, summary = '{}', detail = '{}'")
    reopened = Store(tmp_path)
    assert reopened.analysis_detail(f["id"]) == detail


def test_stopped_time_is_measured_before_the_rollout(recording):
    summary, _ = analyze(recording, default_course())
    assert summary["stopped_sec"] == 0  # a clean roll; its slow finish is in the rollout

    # Hold the buggy still for 20 samples on the Monument Straightaway.
    stall_at = next(i for i, p in enumerate(recording.records) if p.elapsed_sec > 490)
    stalled = [
        p.model_copy(update={"speed_mph": 0.1, "speed_mps": 0.05}) if stall_at <= i < stall_at + 20 else p
        for i, p in enumerate(recording.records)
    ]
    summary, _ = analyze(recording.model_copy(update={"records": stalled}), default_course())
    assert 18 <= summary["stopped_sec"] <= 20
