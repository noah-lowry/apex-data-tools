import pytest
from typer.testing import CliRunner

import apex.collect
from apex.cli import app
from apex.collect import CollectError
from apex.config import (
    config_path,
    load_config,
    normalize_server_url,
    resolve_connection,
    save_config,
    write_server_info,
)

runner = CliRunner()


class Pings(list):
    """Pings recorded instead of sent; set `reject` to make them fail."""

    reject = False


@pytest.fixture
def pings(monkeypatch):
    calls = Pings()

    def ping(url, secret, timeout=5.0):
        calls.append((url, secret))
        if calls.reject:
            raise CollectError(f"{url} rejected the password")

    monkeypatch.setattr(apex.collect, "ping", ping)
    return calls


def test_save_and_load_is_private(tmp_path):
    path = save_config({"server": "http://10.0.0.5:8000", "password": 'a "quoted" pw'})
    assert path == config_path()
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_config() == {"server": "http://10.0.0.5:8000", "password": 'a "quoted" pw'}


def test_normalize_server_url():
    assert normalize_server_url("10.0.0.5:8000/") == "http://10.0.0.5:8000"
    assert normalize_server_url("https://apex.example.org") == "https://apex.example.org"
    with pytest.raises(ValueError):
        normalize_server_url("ftp://x")


def test_precedence(tmp_path):
    data_dir = tmp_path / "srv"
    assert resolve_connection(data_dir=data_dir) is None

    write_server_info(data_dir, "http://127.0.0.1:8000", "local-token")
    local = resolve_connection(data_dir=data_dir)
    assert (local.url, local.secret) == ("http://127.0.0.1:8000", "local-token")

    save_config({"server": "http://10.0.0.5:8000", "password": "pw"})
    configured = resolve_connection(data_dir=data_dir)
    assert (configured.url, configured.secret, configured.source) == ("http://10.0.0.5:8000", "pw", "config")

    flags = resolve_connection("10.0.0.9:9000", "other", data_dir=data_dir)
    assert (flags.url, flags.secret) == ("http://10.0.0.9:9000", "other")


def test_local_token_only_for_the_local_server(tmp_path):
    data_dir = tmp_path / "srv"
    write_server_info(data_dir, "http://127.0.0.1:8000", "local-token")
    save_config({"server": "http://10.0.0.5:8000"})
    assert resolve_connection(data_dir=data_dir).secret is None


def test_set_then_show(pings):
    r = runner.invoke(app, ["config", "set", "server", "10.0.0.5:8000"])
    assert r.exit_code == 0 and "apex config set password" in r.output
    assert pings == []  # nothing to check without a password

    r = runner.invoke(app, ["config", "set", "password"], input="sekrit\n")
    assert r.exit_code == 0, r.output
    assert "sekrit" not in r.output and "password accepted" in r.output
    assert pings == [("http://10.0.0.5:8000", "sekrit")]
    assert load_config() == {"server": "http://10.0.0.5:8000", "password": "sekrit"}

    r = runner.invoke(app, ["config"])
    assert r.exit_code == 0
    assert "http://10.0.0.5:8000" in r.output and "sekrit" not in r.output


def test_show_reports_a_rejected_password(pings):
    save_config({"server": "http://10.0.0.5:8000", "password": "stale"})
    pings.reject = True
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 1 and "rejected" in r.output
    assert runner.invoke(app, ["config", "--no-check"]).exit_code == 0


def test_show_with_nothing_set():
    r = runner.invoke(app, ["config"])
    assert r.exit_code == 1 and "apex config set server" in r.output


def test_set_rejects_bad_values():
    assert runner.invoke(app, ["config", "set", "server", "ftp://x"]).exit_code == 2
    assert runner.invoke(app, ["config", "set", "colour", "red"]).exit_code == 2
    assert load_config() == {}


def test_unset(pings):
    save_config({"server": "http://10.0.0.5:8000", "password": "pw"})
    assert runner.invoke(app, ["config", "unset", "password"]).exit_code == 0
    assert load_config() == {"server": "http://10.0.0.5:8000"}
    assert "was not set" in runner.invoke(app, ["config", "unset", "password"]).output


def test_collect_rolls_uses_config(tmp_path, monkeypatch):
    save_config({"server": "http://10.0.0.5:8000"})
    watch = tmp_path / "w"
    watch.mkdir()
    r = runner.invoke(app, ["collect-rolls", "--no-buggy", str(watch)])
    assert r.exit_code == 2 and "no password for http://10.0.0.5:8000" in r.stderr

    save_config({"server": "http://10.0.0.5:8000", "password": "pw"})
    seen = []
    monkeypatch.setattr(apex.collect, "http_uploader", lambda url, secret: seen.append((url, secret)))
    runner.invoke(app, ["collect-rolls", "--no-buggy", str(watch)])
    assert seen == [("http://10.0.0.5:8000", "pw")]
