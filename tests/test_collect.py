import shutil
from datetime import date

import pytest
from conftest import SAMPLE_FIT

from typer.testing import CliRunner

from apex.cli import app, qr_code
from apex.collect import CollectError, collect, find_fit_files, read_header
from apex.config import read_server_info, write_server_info

SAMPLE_DAY = read_header(SAMPLE_FIT).local_day
runner = CliRunner()


@pytest.fixture
def watch(tmp_path):
    """A mounted-watch layout: one activity, plus a non-activity FIT elsewhere."""
    root = tmp_path / "GARMIN"
    (root / "GARMIN" / "Activity").mkdir(parents=True)
    shutil.copy(SAMPLE_FIT, root / "GARMIN" / "Activity" / "GA373120.FIT")
    (root / "GARMIN" / "Monitor").mkdir()
    (root / "GARMIN" / "Monitor" / "notes.txt").write_text("not fit")
    return root


def uploader_for(client):
    def upload(path, buggy):
        params = {"filename": path.name} | ({"buggy": buggy} if buggy else {})
        r = client.post("/api/files", params=params, content=path.read_bytes())
        r.raise_for_status()
        return r.json()

    return upload


def test_find_fit_files_recurses_any_case(watch):
    assert [p.name for p in find_fit_files(watch)] == ["GA373120.FIT"]
    single = watch / "GARMIN" / "Activity" / "GA373120.FIT"
    assert find_fit_files(single) == [single]


def test_header_gives_kind_and_start():
    wf = read_header(SAMPLE_FIT)
    assert wf.kind == "activity"
    assert wf.created.isoformat() == "2026-10-03T11:31:20+00:00"


def test_only_today_by_default(client, watch):
    report = collect(
        watch, buggy="Solaris", include_all=False, upload=uploader_for(client),
        today=date(2026, 10, 31),
    )
    assert report.outcomes == [] and report.other_days == 1


def test_collect_today_then_relabel(client, watch):
    upload, seen = uploader_for(client), []
    report = collect(watch, buggy=None, include_all=False, upload=upload,
                     on_outcome=seen.append, today=SAMPLE_DAY)
    assert report.count("created") == 1 and seen == report.outcomes
    assert report.outcomes[0].stored["point_count"] == 710

    report = collect(watch, buggy="Solaris", include_all=True, upload=upload)
    assert report.count("buggy_set") == 1
    report = collect(watch, buggy="Kingfisher", include_all=True, upload=upload)
    assert report.count("exists") == 1


def test_collect_reports_failures_and_continues(watch):
    def broken(path, buggy):
        raise RuntimeError("HTTP 500: boom")

    report = collect(watch, buggy="Solaris", include_all=True, upload=broken)
    assert report.count("failed") == 1 and report.outcomes[0].error == "HTTP 500: boom"


def test_collect_with_no_fit_files(tmp_path):
    with pytest.raises(CollectError):
        collect(tmp_path, buggy=None, include_all=True, upload=lambda p, b: {})


def test_cli_requires_exactly_one_buggy_choice(watch):
    result = runner.invoke(app, ["collect-rolls", str(watch)])
    assert result.exit_code == 2 and "--no-buggy" in result.output
    result = runner.invoke(app, ["collect-rolls", "-b", "Solaris", "--no-buggy", str(watch)])
    assert result.exit_code == 2


def test_cli_rejects_missing_dir(tmp_path):
    result = runner.invoke(app, ["collect-rolls", "--no-buggy", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_cli_needs_a_server_or_password(tmp_path, watch):
    result = runner.invoke(
        app, ["collect-rolls", "--data-dir", str(tmp_path / "d"), "--no-buggy", str(watch)]
    )
    assert result.exit_code == 2 and "no server configured" in result.stderr


def test_cli_unreachable_server(tmp_path, watch):
    data_dir = tmp_path / "d"
    write_server_info(data_dir, "http://127.0.0.1:9", "tok")
    assert (data_dir / "server.json").stat().st_mode & 0o777 == 0o600
    assert read_server_info(data_dir)["token"] == "tok"

    result = runner.invoke(
        app, ["collect-rolls", "--data-dir", str(data_dir), "--no-buggy", "--all", str(watch)]
    )
    assert result.exit_code == 1 and "can't reach the server" in result.stderr


def test_cli_end_to_end(tmp_path, watch, client, monkeypatch):
    """The real command, with HTTP routed into the test app."""
    import apex.collect

    monkeypatch.setattr(apex.collect, "http_uploader", lambda url, secret: uploader_for(client))
    args = ["collect-rolls", "--server", "http://x", "--password", "pw", "--all", str(watch)]
    first = runner.invoke(app, [*args, "-b", "Solaris"])
    assert first.exit_code == 0, first.output
    assert "GA373120.FIT" in first.output and "1 new" in first.output

    again = runner.invoke(app, [*args, "--no-buggy"])
    assert "1 already collected" in again.output


def test_qr_code_is_square_with_quiet_zone():
    qr = qr_code("http://192.168.1.20:8000/?key=buggy").plain.splitlines()
    assert len({len(row) for row in qr}) == 1
    assert len(qr[0]) in (2 * len(qr) - 1, 2 * len(qr))  # two modules per row of text
