from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apex.server import create_app
from apex.server.db import Store

SAMPLE_FIT = Path(__file__).parent / "sample_data" / "GA373120.FIT"
PASSWORD = "buggy"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Never read a developer's real config, data dir, or credentials."""
    for var in ("APEX_DATA_DIR", "APEX_PASSWORD", "APEX_SERVER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APEX_CONFIG", str(tmp_path / "config" / "config.toml"))
    monkeypatch.setenv("APEX_DATA_DIR", str(tmp_path / "no-local-server"))


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "data")


@pytest.fixture
def app(tmp_path):
    return create_app(password=PASSWORD, data_dir=tmp_path / "data")


@pytest.fixture
def client(app) -> TestClient:
    """A client holding the bearer password, as the CLI would."""
    return TestClient(app, headers={"Authorization": f"Bearer {PASSWORD}"})
