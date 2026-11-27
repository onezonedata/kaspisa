"""Т29–Т34, Т57: `install.sh`, `make show-key`, блокировка `backups/.lock.d`.

Скрипты запускаются во временной копии репозитория с заглушкой `docker` в PATH (пишет argv в файл).
Значения секретов в сообщения проверок не попадают: сравнения сводятся к булевым флагам.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from deploy_testing import REPO, write_stub

from kaspisa.security.keys import SecuritySettings, load_keyring

HEX64 = re.compile(r"^[0-9a-f]{64}$")
COPIED = [
    ".gitignore",
    "install.sh",
    "update.sh",
    "Makefile",
    "docker-compose.prod.yml",
    "deploy/lib.sh",
    "deploy/restore.sh",
    "backend/pyproject.toml",
]
SECRET_NAMES = [
    "KASPISA_ENCRYPTION_KEY",
    "POSTGRES_PASSWORD",
    "KASPISA_DB_OWNER_PASSWORD",
    "KASPISA_DB_APP_PASSWORD",
]
MAKE = shutil.which("make")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="нужен git")


def _clean_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "KASPISA_"))}
    # Переменные внешнего make: вложенный GNU Make 4.3 иначе печатает «Entering directory».
    for name in ("MAKELEVEL", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
        env.pop(name, None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.test",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.test",
        DOCKER_ARGV=str(tmp_path / "docker-argv.txt"),
    )
    env.update(extra or {})
    return env


def make_project(tmp_path: Path, name: str = "p") -> Path:
    """Временная копия нужных файлов, git с одним коммитом и меткой v0.1.0, заглушка docker."""
    root = tmp_path / name
    root.mkdir()
    for rel in COPIED:
        src = REPO / rel
        if src.exists():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, root / rel)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    write_stub(bindir, "docker", 'printf "%s\\n" "$*" >> "$DOCKER_ARGV"\nexit 0\n')
    assert GIT is not None
    env = _clean_env(tmp_path)
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init", "--no-gpg-sign"],
        ["tag", "v0.1.0"],
    ):
        subprocess.run([GIT, *args], cwd=root, env=env, check=True, capture_output=True)  # noqa: S603
    return root


def run_script(
    root: Path,
    args: list[str],
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Запуск скрипта без терминала (stdin — /dev/null) в очищенном окружении."""
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603
        [bash, *args],
        cwd=root,
        env=_clean_env(root.parent, extra_env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def read_env(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        result[name] = value
    return result


def secret_values(env: dict[str, str]) -> list[str]:
    return [env[name] for name in SECRET_NAMES]


def leaks(text: str, values: list[str]) -> bool:
    """Есть ли в тексте значение секрета или любая его подстрока длиной 12."""
    for value in values:
        for candidate in [value, value.removeprefix("1:")]:
            if candidate in text:
                return True
            if any(candidate[i : i + 12] in text for i in range(len(candidate) - 11)):
                return True
    return False


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_install_creates_env_with_keys(tmp_path: Path) -> None:
    """Т29: `.env` 600, строки режима, ключ в формате связки, три разных пароля, backups 700."""
    root = make_project(tmp_path)
    done = run_script(root, ["install.sh", "tunnel"])
    assert done.returncode == 0, done.stderr
    assert mode_of(root / ".env") == 0o600
    env = read_env(root)
    assert env["COMPOSE_PROFILES"] == "tunnel"
    assert env["KASPISA_DRY_RUN"] == "true"
    assert env["KASPISA_COOKIE_SECURE"] == "false"
    assert env["KASPISA_UPDATE_REPO"] == "onezonedata/kaspisa"
    assert env["KASPISA_IMAGE_PREFIX"] == ""
    assert env["KASPISA_VERSION"] == "v0.1.0"
    assert env["KASPISA_EDGE_SUBNET"] == "172.29.247.0/29"
    assert env["KASPISA_WEB_PORT"] == "3000"
    assert env["KASPISA_UID"] == str(os.getuid())
    assert env["KASPISA_GID"] == str(os.getgid())
    ring = load_keyring(SecuritySettings(encryption_key=env["KASPISA_ENCRYPTION_KEY"]))
    assert ring.primary_id == 1
    assert len(ring.key(1)) == 32
    passwords = [env[n] for n in SECRET_NAMES[1:]]
    assert all(HEX64.match(p) for p in passwords), "пароль не 64 hex-символа"
    assert len(set(passwords)) == 3, "пароли совпадают"
    assert mode_of(root / "backups") == 0o700
    assert not list(root.glob(".env.tmp*"))


def test_install_prints_no_secrets(tmp_path: Path) -> None:
    """Т30: ни вывод, ни аргументы docker не содержат ключа и паролей (и их подстрок)."""
    root = make_project(tmp_path)
    done = run_script(root, ["install.sh", "tunnel"])
    assert done.returncode == 0, done.stderr
    argv = (tmp_path / "docker-argv.txt").read_text(encoding="utf-8")
    assert argv, "docker не вызывался"
    values = secret_values(read_env(root))
    assert not leaks(done.stdout + done.stderr + argv, values), "секрет в выводе или argv docker"


def test_install_is_idempotent(tmp_path: Path) -> None:
    """Т31: повторный запуск не меняет `.env`; другой режим — код 1."""
    root = make_project(tmp_path)
    assert run_script(root, ["install.sh", "tunnel"]).returncode == 0
    before = hashlib.sha256((root / ".env").read_bytes()).hexdigest()
    again = run_script(root, ["install.sh", "tunnel"])
    assert again.returncode == 0, again.stderr
    assert "уже существует" in again.stdout + again.stderr
    assert hashlib.sha256((root / ".env").read_bytes()).hexdigest() == before
    assert mode_of(root / ".env") == 0o600
    other = run_script(root, ["install.sh", "public"])
    assert other.returncode == 1
    assert hashlib.sha256((root / ".env").read_bytes()).hexdigest() == before


def test_install_generates_unique_secrets(tmp_path: Path) -> None:
    """Т32: две установки не делят ни ключ, ни пароли."""
    first = make_project(tmp_path, "a")
    second = make_project(tmp_path, "b")
    assert run_script(first, ["install.sh", "tunnel"]).returncode == 0
    assert run_script(second, ["install.sh", "tunnel"]).returncode == 0
    a, b = secret_values(read_env(first)), secret_values(read_env(second))
    assert not any(x == y for x in a for y in b), "секреты двух установок совпали"


def test_install_public_and_bad_arguments(tmp_path: Path) -> None:
    """Т33: `public` берёт домен из окружения; неверные вызовы не оставляют `.env`."""
    ok = make_project(tmp_path, "ok")
    done = run_script(
        ok,
        ["install.sh", "public"],
        {"KASPISA_DOMAIN": "kaspisa.test", "KASPISA_CADDY_TLS": "internal"},
    )
    assert done.returncode == 0, done.stderr
    env = read_env(ok)
    assert env["COMPOSE_PROFILES"] == "public"
    assert env["KASPISA_COOKIE_SECURE"] == "true"
    assert env["KASPISA_DOMAIN"] == "kaspisa.test"
    assert env["KASPISA_CADDY_TLS"] == "internal"

    for name, args in [
        ("nodomain", ["install.sh", "public"]),
        ("unknown", ["install.sh", "foo"]),
        ("noarg", ["install.sh"]),
    ]:
        root = make_project(tmp_path, name)
        failed = run_script(root, args)
        assert failed.returncode == 1, name
        assert not (root / ".env").exists(), name
        assert not list(root.glob(".env.tmp*")), name
        assert not (root / "backups" / ".lock.d").exists(), name


@pytest.mark.skipif(MAKE is None, reason="нужен make")
def test_make_show_key_prints_only_key(tmp_path: Path) -> None:
    """Т34: `make show-key` печатает ровно значение ключа и перевод строки (ключ одноразовый)."""
    root = make_project(tmp_path)
    assert run_script(root, ["install.sh", "tunnel"]).returncode == 0
    key = read_env(root)["KASPISA_ENCRYPTION_KEY"]
    assert MAKE is not None
    done = subprocess.run(  # noqa: S603
        [MAKE, "show-key"],
        cwd=root,
        env=_clean_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    exact = done.stdout == key + "\n"
    assert done.returncode == 0
    assert exact, "вывод show-key не равен ключу"


def test_clean_env_drops_make_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Окружение вложенного make чистое: GNU Make 4.3 при MAKELEVEL печатает «Entering directory».

    Тест pytest, запущенный из `make test`, наследует эти переменные; на macOS (Make 3.81)
    сбой Т34 не воспроизводится, поэтому проверяется само окружение.
    """
    for name in ("MAKELEVEL", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
        monkeypatch.setenv(name, "1")
    env = _clean_env(tmp_path)
    assert not {"MAKELEVEL", "MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"} & env.keys()


LOCKED = [
    pytest.param(["install.sh", "tunnel"], id="install"),
    pytest.param(["update.sh", "v0.1.0"], id="update"),
    pytest.param(["deploy/restore.sh", "--yes", "backups/x.dump"], id="restore"),
]


@pytest.mark.parametrize("args", LOCKED)
def test_lock_blocks_scripts(tmp_path: Path, args: list[str]) -> None:
    """Т57: занятая блокировка — код 1, ни одного вызова docker, `.env` нет, блокировка цела."""
    root = make_project(tmp_path)
    lock = root / "backups" / ".lock.d"
    lock.mkdir(parents=True)
    (lock / "pid").write_text("99999 other.sh\n", encoding="utf-8")
    done = run_script(root, args)
    assert done.returncode == 1
    assert "уже выполняется" in done.stderr
    assert not (tmp_path / "docker-argv.txt").exists(), "docker вызван при занятой блокировке"
    assert not (root / ".env").exists()
    assert (lock / "pid").exists()


def test_lock_released_after_install(tmp_path: Path) -> None:
    """Т57: без блокировки установка проходит и снимает свою блокировку."""
    root = make_project(tmp_path)
    assert run_script(root, ["install.sh", "tunnel"]).returncode == 0
    assert not (root / "backups" / ".lock.d").exists()
