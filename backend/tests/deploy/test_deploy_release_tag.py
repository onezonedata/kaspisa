"""Т48: `deploy/check-release-tag.sh` сверяет метку выпуска с версией `pyproject.toml`."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from deploy_testing import REPO

SCRIPT = REPO / "deploy" / "check-release-tag.sh"


def make_pyproject(tmp_path: Path, version: str = "0.1.0") -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "kaspisa"\nversion = "{version}"\n', encoding="utf-8")
    return path


def run_check(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_matching_tag_passes(tmp_path: Path) -> None:
    done = run_check(["v0.1.0", str(make_pyproject(tmp_path))])
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "tag",
    ["v0.1.1", "0.1.0", "v0.1", "v0.1.0-rc1", "v0.1.0x", "vv0.1.0", "v0.1.0\nv0.1.0", ""],
)
def test_wrong_tag_fails(tmp_path: Path, tag: str) -> None:
    done = run_check([tag, str(make_pyproject(tmp_path))])
    assert done.returncode != 0
    assert done.stderr.strip()


def test_missing_argument_fails(tmp_path: Path) -> None:
    assert run_check([]).returncode != 0


def test_unreadable_version_fails(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "kaspisa"\n', encoding="utf-8")
    assert run_check(["v0.1.0", str(path)]).returncode != 0


def test_default_is_real_pyproject() -> None:
    """Без второго аргумента берётся `backend/pyproject.toml` репозитория."""
    text = (REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match
    assert run_check([f"v{match.group(1)}"]).returncode == 0
    assert run_check(["v999.0.0"]).returncode != 0
