"""Общие помощники тестов дистрибутива: пути репозитория, `docker compose config`, заглушки."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO / "docker-compose.prod.yml"
START_API = REPO / "backend" / "docker" / "start-api.sh"

DOCKER = shutil.which("docker")

# Несекретные заглушки: значения различны, чтобы искать их по подстроке.
PG_SUPER = "stub-superuser-password-aaaaaaaaaaaaaaaaaaaaaaaa"
PG_OWNER = "stub-owner-password-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PG_APP = "stub-app-password-cccccccccccccccccccccccccccc"
ENC_KEY = "1:stubkeystubkeystubkeystubkeystubkeystubkey000="
STUB_VERSION = "v9.9.9-stub"
MARKER = "REAL-ENV-MARKER-MUST-NOT-APPEAR"


def docker_compose_available() -> bool:
    """Есть ли `docker compose` (демон для `config` не нужен)."""
    if DOCKER is None:
        return False
    done = subprocess.run(  # noqa: S603
        [DOCKER, "compose", "version"], capture_output=True, check=False, timeout=30
    )
    return done.returncode == 0


def stub_env_lines(profile: str) -> list[str]:
    return [
        f"COMPOSE_PROFILES={profile}",
        f"KASPISA_VERSION={STUB_VERSION}",
        "KASPISA_IMAGE_PREFIX=",
        "KASPISA_DRY_RUN=true",
        "KASPISA_COOKIE_SECURE=false",
        f"KASPISA_ENCRYPTION_KEY={ENC_KEY}",
        "KASPISA_ENCRYPTION_OLD_KEYS=",
        f"POSTGRES_PASSWORD={PG_SUPER}",
        f"KASPISA_DB_OWNER_PASSWORD={PG_OWNER}",
        f"KASPISA_DB_APP_PASSWORD={PG_APP}",
        "KASPISA_UID=1000",
        "KASPISA_GID=1000",
        "KASPISA_UPDATE_REPO=",
        "KASPISA_EDGE_SUBNET=172.29.247.0/29",
        "KASPISA_DOMAIN=kaspisa.test",
        "KASPISA_CADDY_TLS=internal",
    ]


def compose_config(tmp_path: Path, profile: str) -> dict[str, Any]:
    """`docker compose config --format json` в очищенном окружении по заглушечному `.env`."""
    assert DOCKER is not None
    project = tmp_path / f"project-{profile}"
    project.mkdir()
    (project / "stub.env").write_text("\n".join(stub_env_lines(profile)) + "\n", encoding="utf-8")
    # Настоящий `.env` рядом с проектом не должен читаться: в него кладётся маркер.
    (project / ".env").write_text(f"KASPISA_VERSION={MARKER}\n", encoding="utf-8")
    # HOME оставляем: плагин compose Docker Desktop ищется через ~/.docker.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "KASPISA_"))}
    done = subprocess.run(  # noqa: S603
        [
            DOCKER,
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-directory",
            str(project),
            "--env-file",
            str(project / "stub.env"),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert MARKER not in done.stdout
    result: dict[str, Any] = json.loads(done.stdout)
    return result


_DURATION = re.compile(r"(\d+(?:\.\d+)?)(h|m|s)")


def duration_seconds(value: str) -> float:
    """Длительность Go из `docker compose config`: `5m0s`, `1h0m0s`, `300s`."""
    units = {"h": 3600, "m": 60, "s": 1}
    parts = _DURATION.findall(value)
    assert parts, value
    return sum(float(number) * units[unit] for number, unit in parts)


def write_stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path
