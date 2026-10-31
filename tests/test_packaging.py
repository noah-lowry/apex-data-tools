"""The installed package must carry every file the server reads at runtime.

A path that works from a checkout but is missing from the wheel only shows up
once someone installs with `uv tool install`, so build the real wheel and look.
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import apex
from apex.parsing.roll_window import DEFAULT_COURSE_PATH
from apex.server.app import WEB_DIR

PACKAGE = Path(apex.__file__).parent
ROOT = PACKAGE.parent.parent


def runtime_files() -> list[str]:
    """Non-Python files the code reads, relative to the package's parent."""
    assets = [p for p in WEB_DIR.rglob("*") if p.is_file()] + [DEFAULT_COURSE_PATH]
    return sorted(str(p.relative_to(PACKAGE.parent)) for p in assets)


def test_runtime_files_live_inside_the_package():
    for path in [WEB_DIR, DEFAULT_COURSE_PATH]:
        assert path.resolve().is_relative_to(PACKAGE.resolve()), path
    assert "apex/server/web/index.html" in runtime_files()


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build")
def test_wheel_contains_runtime_files_and_entry_point(tmp_path):
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)],
        check=True, capture_output=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        entry_points = next(zf.read(n).decode() for n in names if n.endswith("entry_points.txt"))
    missing = [f for f in runtime_files() if f not in names]
    assert not missing, f"not in the wheel: {missing}"
    assert "apex = apex.cli:main" in entry_points
