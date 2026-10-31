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
    assert data["roster"] == {"buggy": ["Solaris"], "driver": ["Mia"]}


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
    assert client.put("/api/files/999/roll", json={"roll_id": roll["id"]}).status_code == 404
    assert client.put(f"/api/files/{file['id']}/roll", json={"roll_id": 999}).status_code == 404

    today = client.get("/api/collection").json()
    assert today["rolls"][1]["file_ids"] == [file["id"]]
    assert [f["id"] for f in today["files"]] == [file["id"]]

    assert client.delete(f"/api/files/{file['id']}/roll").json()["roll_id"] is None


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
