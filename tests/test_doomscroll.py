from pathlib import Path

import pytest
from conftest import SAMPLE_FIT

SAMPLES = sorted(Path(SAMPLE_FIT).parent.glob("*.FIT"))  # four real Solaris rolls


@pytest.fixture
def season(client):
    """Four Solaris rolls with data (Sep 12 x3, Oct 3), one Molotov roll without."""
    rolls = {}
    for path in SAMPLES:
        file = client.post("/api/files", params={"filename": path.name, "buggy": "Solaris"},
                           content=path.read_bytes()).json()["file"]
        roll = client.post("/api/rolls", json={"buggy": "Solaris", "driver": "Mia", "notes": path.stem}).json()
        client.put(f"/api/files/{file['id']}/roll", json={"roll_id": roll["id"]})
        rolls[path.stem] = roll["id"]
    rolls["empty"] = client.post("/api/rolls", json={"buggy": "Molotov", "driver": "Jolie"}).json()["id"]
    return rolls


def test_course(client):
    course = client.get("/api/course").json()
    assert [s["id"] for s in course["segments"]] == ["hill2_drop", "monument_straight", "chute_turn", "chute_rollout"]
    assert len(course["segments"][0]["start_gate"]["line"]) == 2


def test_feed_cards(client, season):
    feed = client.get("/api/feed").json()
    cards = feed["rolls"]
    assert feed["total"] == 4 and all(c["has_data"] for c in cards)  # no-data roll hidden
    assert [c["at_utc"] for c in cards] == sorted((c["at_utc"] for c in cards), reverse=True)

    totals = {c["id"]: c["total_sec"] for c in cards if c["complete"]}
    best_id = min(totals, key=totals.get)
    best = next(c for c in cards if c["id"] == best_id)
    assert "pb" in best["badges"] and best["gap_sec"] is None and best["reference"] is None
    for c in cards:
        if c["id"] != best_id and c["complete"]:
            assert c["reference"]["id"] == best_id
            assert c["gap_sec"] == pytest.approx(c["total_sec"] - totals[best_id], abs=0.001)
            # Segment gaps add up to the total gap: segments tile the roll.
            assert sum(s["gap_sec"] for s in c["segments"]) == pytest.approx(c["gap_sec"], abs=0.01)
    by_day = {}
    for c in cards:
        if c["complete"]:
            by_day.setdefault(c["day"], []).append(c)
    for cs in by_day.values():
        fastest = min(cs, key=lambda c: c["total_sec"])
        assert "fastest_day" in fastest["badges"]


def test_feed_filters_and_pages(client, season):
    assert client.get("/api/feed", params={"all": True}).json()["total"] == 5
    assert client.get("/api/feed", params={"buggy": "molotov", "all": True}).json()["total"] == 1
    assert client.get("/api/feed", params={"driver": "Nobody"}).json()["total"] == 0
    day = client.get("/api/feed").json()["options"]["days"][0]
    assert all(c["day"] == day for c in client.get("/api/feed", params={"day": day}).json()["rolls"])

    first = client.get("/api/feed", params={"limit": 3}).json()
    assert len(first["rolls"]) == 3 and first["next_cursor"] == 3 and "leaderboard" in first
    second = client.get("/api/feed", params={"limit": 3, "cursor": 3}).json()
    assert len(second["rolls"]) == 1 and second["next_cursor"] is None and "leaderboard" not in second
    assert {c["id"] for c in first["rolls"]}.isdisjoint(c["id"] for c in second["rolls"])


def test_leaderboard(client, season):
    board = client.get("/api/feed").json()["leaderboard"]
    totals = [r["total_sec"] for r in board["fastest"]]
    assert totals == sorted(totals)
    (solaris,) = board["buggies"]
    assert solaris["buggy"] == "Solaris" and solaris["rolls"] == 4
    assert solaris["best"]["total_sec"] == totals[0]
    assert "theoretical_sec" not in solaris
    assert solaris["median_sec"] >= solaris["best"]["total_sec"]


def test_roll_page(client, season):
    cards = client.get("/api/feed").json()["rolls"]
    best = next(c for c in cards if "pb" in c["badges"])
    other = next(c for c in cards if c["complete"] and c["id"] != best["id"])

    page = client.get(f"/api/rolls/{other['id']}/analysis").json()
    assert page["reference"]["id"] == best["id"] and page["reference"]["label"] == "best"
    assert page["analysis"]["trace"]["t"][0] == 0 and page["file"]["filename"].endswith(".FIT")
    assert page["stats"]["total"]["rank"] > 1 and page["stats"]["total"]["best_sec"] == best["total_sec"]
    assert len(page["peers"]) == 4 and len(page["candidates"]) == 3
    assert set(page["stats"]["segments"]) == {"hill2_drop", "monument_straight", "chute_turn", "chute_rollout"}

    mine = client.get(f"/api/rolls/{best['id']}/analysis").json()
    assert mine["stats"]["total"]["rank"] == 1 and mine["reference"]["label"] == "next best"

    assert client.get(f"/api/rolls/{season['empty']}/analysis").json()["analysis"] is None
    assert client.get("/api/rolls/999/analysis").status_code == 404
