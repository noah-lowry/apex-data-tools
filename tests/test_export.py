import hashlib

import pytest
from conftest import SAMPLE_FIT
from typer.testing import CliRunner

from apex.cli import app
from apex.collect import CollectError, read_header
from apex.export import ServerFiles, export_day

SAMPLE_DAY = read_header(SAMPLE_FIT).local_day.isoformat()
runner = CliRunner()


def upload(client, path=SAMPLE_FIT, filename=None, buggy="Solaris", content=None):
    params = {"filename": filename or path.name, "buggy": buggy}
    r = client.post("/api/files", params=params, content=content or path.read_bytes())
    r.raise_for_status()
    return r.json()["file"]


def files_for(client) -> ServerFiles:
    def list_day(day):
        r = client.get("/api/files", params={"day": day})
        r.raise_for_status()
        return r.json()["files"]

    def download(file_id):
        r = client.get(f"/api/files/{file_id}/raw")
        r.raise_for_status()
        return r.content

    return ServerFiles(list_day, download)


def test_raw_download_is_byte_for_byte(client):
    file = upload(client)
    r = client.get(f"/api/files/{file['id']}/raw")
    assert r.status_code == 200 and r.content == SAMPLE_FIT.read_bytes()
    assert "GA373120.FIT" in r.headers["content-disposition"]
    assert client.get("/api/files/999/raw").status_code == 404


def test_list_files_by_day(client):
    file = upload(client)
    assert [f["id"] for f in client.get("/api/files", params={"day": SAMPLE_DAY}).json()["files"]] == [file["id"]]
    assert client.get("/api/files", params={"day": "1999-01-01"}).json()["files"] == []
    assert client.get("/api/files", params={"day": "yesterday"}).status_code == 422


def test_export_saves_then_skips(client, tmp_path):
    upload(client)
    dest = tmp_path / "out"
    first = export_day(dest, day=SAMPLE_DAY, files=files_for(client))
    assert first.count("saved") == 1
    assert (dest / "GA373120.FIT").read_bytes() == SAMPLE_FIT.read_bytes()

    again = export_day(dest, day=SAMPLE_DAY, files=files_for(client))
    assert again.count("exists") == 1 and again.count("saved") == 0
    assert sorted(p.name for p in dest.iterdir()) == ["GA373120.FIT"]


def test_export_nothing_creates_no_folder(client, tmp_path):
    report = export_day(tmp_path / "out", day="1999-01-01", files=files_for(client))
    assert report.outcomes == [] and not (tmp_path / "out").exists()


def test_same_name_different_bytes_gets_the_id(client, tmp_path):
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "GA373120.FIT").write_bytes(b"someone else's recording")
    file = upload(client)
    report = export_day(dest, day=SAMPLE_DAY, files=files_for(client))
    assert report.outcomes[0].path == dest / f"GA373120-{file['id']}.FIT"
    assert report.outcomes[0].path.read_bytes() == SAMPLE_FIT.read_bytes()
    assert (dest / "GA373120.FIT").read_bytes() == b"someone else's recording"


def test_corrupt_download_is_not_written(client, tmp_path):
    upload(client)
    real = files_for(client)
    files = ServerFiles(real.list_day, lambda file_id: b"truncated")
    report = export_day(tmp_path / "out", day=SAMPLE_DAY, files=files)
    assert report.count("failed") == 1 and "checksum" in report.outcomes[0].error
    assert list((tmp_path / "out").iterdir()) == []


def test_unreachable_server_stops_export(tmp_path):
    def down(day):
        raise CollectError("can't reach the server")

    with pytest.raises(CollectError):
        export_day(tmp_path, day=SAMPLE_DAY, files=ServerFiles(down, lambda i: b""))


def test_cli_end_to_end(client, tmp_path, monkeypatch):
    import apex.export

    upload(client)
    monkeypatch.setattr(apex.export, "http_files", lambda url, secret: files_for(client))
    dest = tmp_path / "out"
    args = ["export-fits", str(dest), "--day", SAMPLE_DAY, "--server", "http://x", "--password", "pw"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "GA373120.FIT" in result.output and "1 saved" in result.output
    digest = hashlib.sha256((dest / "GA373120.FIT").read_bytes()).hexdigest()
    assert digest == hashlib.sha256(SAMPLE_FIT.read_bytes()).hexdigest()

    assert "1 already in" in runner.invoke(app, args).output
    empty = runner.invoke(app, [*args[:2], "--day", "1999-01-01", *args[4:]])
    assert empty.exit_code == 0 and "No watch files" in empty.output


def test_cli_rejects_a_bad_day(tmp_path):
    result = runner.invoke(app, ["export-fits", "--day", "11/07/26", "--server", "http://x", "--password", "pw"])
    assert result.exit_code == 2


def test_cli_needs_a_server(tmp_path):
    result = runner.invoke(app, ["export-fits", str(tmp_path / "out"), "--data-dir", str(tmp_path / "d")])
    assert result.exit_code == 2 and "no server configured" in result.stderr
