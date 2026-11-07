import pytest
from conftest import PASSWORD, SAMPLE_FIT
from fastapi.testclient import TestClient

from apex.server import create_app
from apex.server.auth import WeakPassword


def upload(client, buggy=None, content=None, filename="GA373120.FIT"):
    params = {"filename": filename} | ({"buggy": buggy} if buggy else {})
    return client.post("/api/files", params=params, content=SAMPLE_FIT.read_bytes() if content is None else content)


def test_no_unprotected_mode(tmp_path):
    for bad in ("", "   ", "abc", " padded "):
        with pytest.raises(WeakPassword):
            create_app(password=bad, data_dir=tmp_path)


def test_everything_but_login_requires_auth(app):
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/api/collection").status_code == 401
    assert anon.post("/api/rolls", json={"buggy": "Solaris"}).status_code == 401
    assert anon.get("/").status_code == 303
    assert anon.get("/login").status_code == 200
    assert anon.get("/static/apex.css").status_code == 200

    wrong = TestClient(app, headers={"Authorization": "Bearer nope"})
    assert wrong.get("/api/collection").status_code == 401


def test_browser_login_sets_session(app):
    browser = TestClient(app, follow_redirects=False)
    assert browser.post("/login", data={"password": "wrong"}).headers["location"] == "/login?error=1"

    r = browser.post("/login", data={"password": PASSWORD, "next": "//evil.example"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert browser.get("/api/collection").status_code == 200
    assert browser.get("/").status_code == 200


def test_local_token_authenticates(app):
    token = app.state.auth.local_token
    cli = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert cli.get("/api/collection").status_code == 200


def test_create_roll_and_list(client):
    r = client.post("/api/rolls", json={"buggy": "  Solaris ", "driver": "", "notes": " fast "})
    assert r.status_code == 201
    roll = r.json()
    assert roll["buggy"] == "Solaris" and roll["driver"] is None and roll["notes"] == "fast"
    assert roll["title"].startswith("Solaris Roll 1 (")

    second = client.post("/api/rolls", json={"buggy": "Solaris", "driver": "Mia"}).json()
    assert second["number"] == 2

    data = client.get("/api/collection").json()
    assert [r["id"] for r in data["rolls"]] == [second["id"], roll["id"]]
    assert data["roster"] == {"buggy": ["Solaris"], "driver": ["Mia"], "pusher": []}
    assert roll["pushers"] == [None] * 5


def test_roll_requires_buggy(client):
    assert client.post("/api/rolls", json={"driver": "Mia"}).status_code == 422
    assert client.post("/api/rolls", json={"buggy": "   "}).status_code == 422


def test_upload_dedups_and_respects_existing_label(client):
    first = upload(client, buggy="Solaris").json()
    assert first["status"] == "created" and first["file"]["buggy"] == "Solaris"
    assert upload(client, buggy="Kingfisher").json()["status"] == "exists"
    assert upload(client, content=b"").status_code == 400


def test_assign_flow(client):
    roll = client.post("/api/rolls", json={"buggy": "Solaris"}).json()
    other = client.post("/api/rolls", json={"buggy": "Kingfisher"}).json()
    file = upload(client, buggy="Solaris").json()["file"]

    # Sample is from another day: hidden by default, visible with all=true.
    assert client.get("/api/collection").json()["files"] == []
    assert len(client.get("/api/collection", params={"all": True}).json()["files"]) == 1

    r = client.put(f"/api/files/{file['id']}/roll", json={"roll_id": roll["id"]})
    assert r.status_code == 200 and r.json()["roll_id"] == roll["id"]
    assert client.put(f"/api/files/{file['id']}/roll", json={"roll_id": other["id"]}).status_code == 409
    second = upload(client, content=b"another recording", filename="b.FIT").json()["file"]
    r = client.put(f"/api/files/{second['id']}/roll", json={"roll_id": roll["id"]})
    assert r.status_code == 409 and "already has data" in r.json()["detail"]
    assert client.put("/api/files/999/roll", json={"roll_id": roll["id"]}).status_code == 404
    assert client.put(f"/api/files/{file['id']}/roll", json={"roll_id": 999}).status_code == 404

    # The roll now happened when its data says: back on the sample's day, 10/03.
    today = client.get("/api/collection").json()
    assert [r["id"] for r in today["rolls"]] == [other["id"]]
    assert [f["id"] for f in today["files"]] == [second["id"]]  # unparseable, so dated today
    moved = next(r for r in client.get("/api/collection", params={"all": True}).json()["rolls"]
                 if r["id"] == roll["id"])
    assert moved["file_ids"] == [file["id"]]
    assert moved["at_utc"] == moved["data_start_utc"] == file["roll_start_utc"]
    assert moved["day"] == file["day"] and moved["title"].endswith("(10/03/26)")

    # Unassigning puts it back on the day it was logged.
    assert client.delete(f"/api/files/{file['id']}/roll").json()["roll_id"] is None
    assert {r["id"] for r in client.get("/api/collection").json()["rolls"]} == {roll["id"], other["id"]}


def test_qr_key_signs_in_and_leaves_the_url(app):
    phone = TestClient(app, follow_redirects=False)
    r = phone.get("/", params={"key": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "apex_session" in r.cookies
    assert phone.get("/api/collection").status_code == 200

    stranger = TestClient(app, follow_redirects=False)
    r = stranger.get("/", params={"key": "guess"})
    assert r.headers["location"] == "/login?error=1"
    assert stranger.get("/api/collection").status_code == 401


def test_edit_roll(client):
    roll = client.post("/api/rolls", json={"buggy": "Solaros", "driver": "Mia", "notes": "x"}).json()
    client.post("/api/rolls", json={"buggy": "Solaris"})

    r = client.put(f"/api/rolls/{roll['id']}", json={"buggy": "Solaris", "driver": None, "notes": "fixed"})
    assert r.status_code == 200
    edited = r.json()
    assert (edited["buggy"], edited["driver"], edited["notes"]) == ("Solaris", None, "fixed")
    assert edited["created_utc"] == roll["created_utc"] and edited["edited_utc"]
    # Created first, so it is now Solaris Roll 1 and the other one is Roll 2.
    assert edited["number"] == 1
    assert [r["number"] for r in client.get("/api/collection").json()["rolls"]] == [2, 1]
    # The typo no longer appears in the dropdowns; neither does the dropped driver.
    assert client.get("/api/collection").json()["roster"] == {"buggy": ["Solaris"], "driver": [], "pusher": []}

    assert client.put(f"/api/rolls/{roll['id']}", json={"buggy": " "}).status_code == 422
    assert client.put("/api/rolls/999", json={"buggy": "Solaris"}).status_code == 404


def test_roll_pushers(client):
    roll = client.post(
        "/api/rolls", json={"buggy": "Solaris", "pushers": [" Ana ", None, "", "Bo"]}
    ).json()
    assert roll["pushers"] == ["Ana", None, None, "Bo", None]
    assert client.get("/api/collection").json()["roster"]["pusher"] == ["Ana", "Bo"]

    # Replacing the roll replaces its pushers; a dropped name leaves the roster.
    edited = client.put(
        f"/api/rolls/{roll['id']}", json={"buggy": "Solaris", "pushers": [None, "ana"] + [None] * 3}
    ).json()
    assert edited["pushers"] == [None, "Ana", None, None, None]
    assert client.get("/api/collection").json()["roster"]["pusher"] == ["Ana"]

    assert client.post("/api/rolls", json={"buggy": "Solaris", "pushers": ["a"] * 6}).status_code == 422
    assert client.post("/api/rolls", json={"buggy": "Solaris", "pushers": ["x" * 81]}).status_code == 422


def test_delete_roll_unassigns_its_data(client):
    roll = client.post("/api/rolls", json={"buggy": "Solaris"}).json()
    file = upload(client).json()["file"]
    client.put(f"/api/files/{file['id']}/roll", json={"roll_id": roll["id"]})

    assert client.delete(f"/api/rolls/{roll['id']}").status_code == 204
    assert client.delete(f"/api/rolls/{roll['id']}").status_code == 404
    data = client.get("/api/collection", params={"all": True}).json()
    assert data["rolls"] == [] and data["files"][0]["roll_id"] is None
