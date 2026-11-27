"""Правки по ревью этапа 9: окружение оболочки не перекрывает `.env`, сети, порты, версия Docker,
класс копий `update`, единый источник версии.

Скрипты запускаются в копии репозитория с заглушкой `docker` (пишет argv и окружение в файлы).
"""

from __future__ import annotations

import importlib.metadata
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from deploy_testing import REPO, compose_config, docker_compose_available, write_stub
from test_deploy_backup import Box
from test_deploy_install import GIT, make_project, run_script

import kaspisa

pytestmark_git = pytest.mark.skipif(GIT is None, reason="нужен git")
needs_compose = pytest.mark.skipif(not docker_compose_available(), reason="нет docker compose")

SENTINEL = "SHELL-ENV-SENTINEL"
SHELL_ENV = {
    "KASPISA_DRY_RUN": f"false-{SENTINEL}",
    "KASPISA_ENCRYPTION_KEY": f"1:{SENTINEL}",
    "POSTGRES_PASSWORD": SENTINEL,
    "KASPISA_DB_APP_PASSWORD": SENTINEL,
    "KASPISA_VERSION": f"v-{SENTINEL}",
    "COMPOSE_PROFILES": f"public-{SENTINEL}",
}
RECORD_ENV = (
    'printf "%s\\n" "$*" >> "$DOCKER_ARGV"\n'
    # Окружение пишется только для вызовов compose из скриптов (`-f docker-compose.prod.yml`).
    'case "$*" in *docker-compose.prod.yml*) echo "call" >> "$DOCKER_ARGV.env"\n'
    "env | grep -E '^(KASPISA_|POSTGRES_|COMPOSE_)' | sort >> \"$DOCKER_ARGV.env\" ;; esac\n"
    "exit 0\n"
)


def recorded_env(tmp_path: Path) -> str:
    path = tmp_path / "docker-argv.txt.env"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- Окружение оболочки не перекрывает .env ---


@pytestmark_git
def test_install_ignores_shell_env_for_compose(tmp_path: Path) -> None:
    """Переменные оболочки (DRY_RUN, ключ, пароли) не доходят до compose: значения берёт `.env`."""
    root = make_project(tmp_path)
    write_stub(tmp_path / "bin", "docker", RECORD_ENV)
    done = run_script(root, ["install.sh", "tunnel"], SHELL_ENV)
    assert done.returncode == 0, done.stderr
    seen = recorded_env(tmp_path)
    assert "COMPOSE_PROFILES" not in seen
    assert SENTINEL not in seen, "значение из оболочки дошло до compose"


@pytestmark_git
def test_restore_ignores_shell_env_for_compose(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    write_stub(tmp_path / "bin", "docker", RECORD_ENV)
    (root / "backups").mkdir(exist_ok=True)
    name = "daily-20250101T000000Z.dump"
    (root / "backups" / name).write_text("x", encoding="utf-8")
    done = run_script(root, ["deploy/restore.sh", "--yes", f"backups/{name}"], SHELL_ENV)
    assert done.returncode == 0, done.stderr
    assert recorded_env(tmp_path), "docker не вызывался"
    assert SENTINEL not in recorded_env(tmp_path)


@pytestmark_git
def test_compose_set_passes_only_explicit_overrides(tmp_path: Path) -> None:
    """`KASPISA_COMPOSE_SET` (так работают compose_new*) попадает в compose, остальное — нет."""
    root = make_project(tmp_path)
    write_stub(tmp_path / "bin", "docker", RECORD_ENV)
    done = run_script(
        root,
        [
            "-c",
            "source deploy/lib.sh; KASPISA_COMPOSE_SET=(KASPISA_VERSION=v1 KASPISA_DRY_RUN=true); "
            "kaspisa_compose ps; kaspisa_compose ps",
        ],
        SHELL_ENV,
    )
    assert done.returncode == 0, done.stderr
    seen = recorded_env(tmp_path)
    assert "KASPISA_VERSION=v1" in seen
    assert "KASPISA_DRY_RUN=true" in seen
    assert SENTINEL not in seen


# --- Установка: демон и версия Docker ---


@pytestmark_git
def test_install_checks_docker_daemon_before_env(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    body = 'case "$1" in info) exit 1 ;; esac\nexit 0\n'
    write_stub(tmp_path / "bin", "docker", body)
    done = run_script(root, ["install.sh", "tunnel"])
    assert done.returncode == 1
    assert "docker" in done.stderr
    assert not (root / ".env").exists()
    assert not (root / "backups" / ".lock.d").exists()


def docker_with_version(tmp_path: Path, version: str) -> None:
    body = f'case "$1" in version) echo {version}; exit 0 ;; esac\nexit 0\n'
    write_stub(tmp_path / "bin", "docker", body)


@pytestmark_git
def test_install_public_requires_docker_28(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    docker_with_version(tmp_path, "27.5.1")
    env = {"KASPISA_DOMAIN": "kaspisa.test", "KASPISA_CADDY_TLS": "internal"}
    done = run_script(root, ["install.sh", "public"], env)
    assert done.returncode == 1
    assert "28" in done.stderr
    assert not (root / ".env").exists()


@pytestmark_git
def test_install_public_warns_when_docker_version_unreadable(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    body = 'case "$1" in version) exit 1 ;; esac\nexit 0\n'
    write_stub(tmp_path / "bin", "docker", body)
    env = {"KASPISA_DOMAIN": "kaspisa.test", "KASPISA_CADDY_TLS": "internal"}
    done = run_script(root, ["install.sh", "public"], env)
    assert done.returncode == 0, done.stderr
    assert "версию Docker" in done.stderr
    assert (root / ".env").exists()


@pytestmark_git
@pytest.mark.parametrize(
    ("mode", "version"),
    [("public", "28.0.1"), ("public", "29.1.0"), ("tunnel", "27.5.1"), ("local", "20.10.7")],
)
def test_install_docker_version_ok(tmp_path: Path, mode: str, version: str) -> None:
    root = make_project(tmp_path)
    docker_with_version(tmp_path, version)
    env = {"KASPISA_DOMAIN": "kaspisa.test", "KASPISA_CADDY_TLS": "internal"}
    done = run_script(root, ["install.sh", mode], env)
    assert done.returncode == 0, done.stderr


# --- Compose: сети и порты ---


@needs_compose
@pytest.mark.parametrize("profile", ["local", "tunnel", "public"])
def test_web_is_isolated_from_db_and_redis(tmp_path: Path, profile: str) -> None:
    services = compose_config(tmp_path, profile)["services"]
    web = set(services["web"]["networks"])
    assert "default" not in web
    for name in ("db", "redis", "worker", "backup"):
        assert not web & set(services[name].get("networks", {"default": None})), name
    assert web & set(services["api"]["networks"]), "web не достаёт до api"


@needs_compose
def test_caddy_publishes_on_ipv4_only(tmp_path: Path) -> None:
    services = compose_config(tmp_path, "public")["services"]
    for port in services["caddy"]["ports"]:
        assert port["host_ip"] == "0.0.0.0", port  # noqa: S104


# --- prepare-server ---


def test_prepare_server_opens_actual_sshd_ports() -> None:
    text = (REPO / "deploy" / "prepare-server.sh").read_text(encoding="utf-8")
    code = [x for x in text.splitlines() if not x.lstrip().startswith("#")]
    joined = "\n".join(code)
    assert "sshd -T" in joined
    assert "listenaddress" in joined
    assert "ssh.socket" in joined
    assert "ufw allow OpenSSH" not in joined
    enable = next(i for i, x in enumerate(code) if "ufw --force enable" in x)
    allow = next(i for i, x in enumerate(code) if "ufw allow" in x and "ssh" in x.lower())
    assert allow < enable


# --- Копии: класс update ---


def test_update_class_has_own_rotation(tmp_path: Path) -> None:
    box = Box(tmp_path)
    manual = [box.put("manual", timedelta(days=d)) for d in (1, 2, 3)]
    for _ in range(4):
        assert box.run("update").returncode == 0
    assert len(box.names("update-")) == 3
    assert all(p.exists() for p in manual), "копия update вытеснила ручную"
    assert box.run("manual").returncode == 0
    assert len(box.names("update-")) == 3


def test_restore_accepts_update_copy(tmp_path: Path) -> None:
    box = Box(tmp_path)
    name = f"update-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.dump"
    (box.dir / name).write_text("x", encoding="utf-8")
    done = box.run("restore", f"{box.dir}/{name}")
    assert done.returncode == 0, done.stderr


# --- update.sh: статика ---


def update_code() -> str:
    text = (REPO / "update.sh").read_text(encoding="utf-8")
    return "\n".join(x for x in text.splitlines() if not x.lstrip().startswith("#"))


def test_update_uses_update_class_and_recreates_all_services() -> None:
    code = update_code()
    assert "backup.sh update" in code
    assert "backup.sh manual" not in code
    # Успешный путь пересоздаёт backup (и caddy в public), а не только перезапускает caddy.
    assert "up -d --no-build backup" in code
    assert "up -d --no-build caddy" in code


def test_update_nothing_can_fail_after_live_worker() -> None:
    """После запуска боевого worker откат с восстановлением базы невозможен: между ним и
    `STAGE='done'` нет команд compose (их сбой или Ctrl-C откатил бы базу и потерял записи цен)."""
    code = update_code()
    live = code.index("compose_new up -d --no-build worker")
    done = code.index("STAGE='done'")
    between = code[live:done]
    assert "compose" not in between.split("\n", 1)[1]
    # Сигналы игнорируются уже во время самого `up worker`: Ctrl-C не откатит базу.
    assert 0 < live - code.rindex("trap '' INT TERM", 0, live) < 40
    assert code.index("up -d --no-build backup") < live
    assert code.index("up -d --no-build caddy") < live
    assert code.rindex("restart caddy") < live


def test_update_interrupt_in_write_env_cleans_tmp() -> None:
    code = update_code()
    assert 'rm -f "$ROOT/.env.tmp.update.$$"' in code
    # До записи версии сигналы игнорируются.
    done_at = code.index("STAGE='done'")
    assert "trap '' INT TERM" in code[done_at - 200 : done_at + 400]


# --- Единый источник версии ---


def test_version_has_single_source() -> None:
    assert kaspisa.__version__ == importlib.metadata.version("kaspisa")
    init = (REPO / "backend" / "src" / "kaspisa" / "__init__.py").read_text(encoding="utf-8")
    assert "metadata" in init
    assert '"0.1.0"' not in init
