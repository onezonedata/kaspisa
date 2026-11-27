"""Т51 и статика Ч6: `prepare-server.sh` и workflow.

Скрипт не запускается: ему нужны внешние репозитории и права root.
"""

from __future__ import annotations

import subprocess

from deploy_testing import REPO

PREPARE = REPO / "deploy" / "prepare-server.sh"
WORKFLOWS = REPO / ".github" / "workflows"


def lines() -> list[str]:
    return PREPARE.read_text(encoding="utf-8").splitlines()


def first_index(needle: str) -> int:
    for i, line in enumerate(lines()):
        if not line.lstrip().startswith("#") and needle in line:
            return i
    raise AssertionError(f"нет команды {needle!r}")


def test_syntax_ok() -> None:
    done = subprocess.run(  # noqa: S603
        ["bash", "-n", str(PREPARE)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr


def test_ssh_allowed_before_ufw_enable() -> None:
    assert first_index('ufw allow "$ssh_port/tcp"') < first_index("ufw --force enable")
    assert first_index("ufw default deny incoming") < first_index("ufw --force enable")


def test_web_ports_only_with_public_flag() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert text.count("80/tcp") + text.count("443/tcp") >= 2
    for needle in ("ufw allow 80/tcp", "ufw allow 443/tcp"):
        idx = first_index(needle)
        # Выше по тексту открывающей `if` должна стоять проверка флага public.
        head = "\n".join(lines()[max(0, idx - 3) : idx])
        assert "PUBLIC" in head, needle


def test_docker_install_is_skipped_when_present() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert "command -v docker" in text
    assert "docker compose version" in text


def test_tailscale_is_optional_and_not_logged_in() -> None:
    text = PREPARE.read_text(encoding="utf-8")
    assert "--tailscale" in text
    assert "tailscale serve --bg 3000" in text
    assert "tailscale up" not in "\n".join(
        line
        for line in lines()
        if not line.lstrip().startswith("#") and "echo" not in line and "printf" not in line
    )


def test_workflows_exist_with_required_parts() -> None:
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    needles = ("workflow_call", "postgres:17", "redis:7", "make lint", "make test")
    for needle in needles:
        assert needle in ci, needle
    # Заявки в зеркало не принимаются: CI не запускается на коде чужого pull request.
    assert "pull_request" not in ci
    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    for needle in (
        "check-release-tag.sh",
        "linux/amd64,linux/arm64",
        "ghcr.io",
        "kaspisa-backend",
        "kaspisa-web",
        "--verify-tag",
        "./.github/workflows/ci.yml",
    ):
        assert needle in release, needle
    assert ":latest" not in release
