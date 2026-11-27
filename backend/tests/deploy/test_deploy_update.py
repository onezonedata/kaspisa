"""Т43 (быстрая часть) и предусловия `update.sh` (ADR 0023): отказ до любых действий.

Полный путь обновления (миграции, откат, сигналы) проверяет `deploy/check/update.sh` на настоящем
docker. Здесь `docker` — заглушка, которая пишет argv в файл: проверяется, что при отказе
ни одна команда, меняющая состояние, не вызывалась, а блокировка снята и журнал закрыт правами 600.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from deploy_testing import REPO, write_stub
from test_deploy_install import (
    GIT,
    _clean_env,
    leaks,
    make_project,
    mode_of,
    run_script,
    secret_values,
)

pytestmark = pytest.mark.skipif(GIT is None, reason="нужен git")

TOOLS = ("uv", "node", "npm")
MUTATING = ("stop", "up", "run", "pull", "build", "restart", "exec")


def installed(tmp_path: Path) -> Path:
    """Проект с `.env` от настоящего `install.sh` (docker — заглушка)."""
    root = make_project(tmp_path)
    done = run_script(root, ["install.sh", "tunnel"])
    assert done.returncode == 0, done.stderr
    (tmp_path / "docker-argv.txt").unlink()
    # Режим сборки требует uv, node и npm в PATH: заглушки не зависят от машины.
    for tool in TOOLS:
        write_stub(tmp_path / "bin", tool, "exit 0\n")
    return root


def docker_calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "docker-argv.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def head(root: Path) -> str:
    assert GIT is not None
    done = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "HEAD"],
        cwd=root,
        env=_clean_env(root.parent),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def assert_nothing_changed(tmp_path: Path, root: Path) -> None:
    for line in docker_calls(tmp_path):
        words = line.split()
        assert not any(w in MUTATING for w in words[:6]), f"изменяющий вызов docker: {line}"
    assert not (root / "backups" / ".lock.d").exists(), "блокировка не снята"
    assert not list(root.glob(".env.tmp*"))


def test_update_dirty_tree_refuses(tmp_path: Path) -> None:
    """Т43: изменён отслеживаемый файл → код 1, HEAD и `.env` прежние, docker ничего не менял."""
    root = installed(tmp_path)
    env_before = hashlib.sha256((root / ".env").read_bytes()).hexdigest()
    head_before = head(root)
    with (root / "Makefile").open("a", encoding="utf-8") as handle:
        handle.write("# правка владельца\n")
    done = run_script(root, ["update.sh", "v0.1.0"])
    assert done.returncode == 1, done.stderr
    assert "git status" in done.stdout + done.stderr
    assert head(root) == head_before
    assert hashlib.sha256((root / ".env").read_bytes()).hexdigest() == env_before
    assert_nothing_changed(tmp_path, root)
    logs = list((root / "backups").glob("update-*.log"))
    assert len(logs) == 1, "журнал обновления не создан"
    assert mode_of(logs[0]) == 0o600
    text = logs[0].read_text(encoding="utf-8") + done.stdout + done.stderr
    assert not leaks(text, secret_values_of(root)), "секрет в журнале или выводе"


def secret_values_of(root: Path) -> list[str]:
    from test_deploy_install import read_env

    return secret_values(read_env(root))


def test_update_without_env_refuses(tmp_path: Path) -> None:
    """Нет `.env` (установки нет) → код 1 без вызовов docker, меняющих состояние."""
    root = make_project(tmp_path)
    done = run_script(root, ["update.sh", "v0.1.0"])
    assert done.returncode == 1
    assert ".env" in done.stdout + done.stderr
    assert_nothing_changed(tmp_path, root)


@pytest.mark.parametrize("registry", [False, True])
@pytest.mark.parametrize("ref", ["--upload-pack=x", "a b", ""])
def test_update_rejects_bad_ref(tmp_path: Path, ref: str, registry: bool) -> None:
    """Опции git, пробелы и пустая метка не проходят ни в одном режиме."""
    root = installed(tmp_path)
    if registry:
        set_registry(root)
    done = run_script(root, ["update.sh", ref] if ref else ["update.sh"])
    assert done.returncode == 1, done.stderr
    assert_nothing_changed(tmp_path, root)


@pytest.mark.parametrize("ref", ["main", "v0.1", "v0.1.0-rc1"])
def test_update_registry_requires_release_tag(tmp_path: Path, ref: str) -> None:
    """В режиме реестра метка — строго `vX.Y.Z`: образа с другим тегом нет."""
    root = installed(tmp_path)
    set_registry(root)
    done = run_script(root, ["update.sh", ref])
    assert done.returncode == 1, done.stderr
    assert "vX.Y.Z" in done.stdout + done.stderr
    assert_nothing_changed(tmp_path, root)


def set_registry(root: Path) -> None:
    """`.env` в режиме реестра (префикс образов задан), права и остальные строки прежние."""
    lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    prefix = "KASPISA_IMAGE_PREFIX="
    out = [prefix + "localhost:1/kaspisa" if line.startswith(prefix) else line for line in lines]
    (root / ".env").write_text("\n".join(out) + "\n", encoding="utf-8")


def test_update_structure() -> None:
    """Тело — функция `main`, вызов в последней строке; HUP игнорируется; трассировки нет."""
    text = (REPO / "update.sh").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[-1] == 'main "$@"; exit $?'
    code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    assert "trap '' HUP" in code
    assert "set -x" not in code
    assert "source .env" not in code and ". .env" not in code
    assert shutil.which("bash") is not None


def git_run(root: Path, *args: str) -> str:
    assert GIT is not None
    done = subprocess.run(  # noqa: S603
        [GIT, *args],
        cwd=root,
        env=_clean_env(root.parent),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def with_origin_release(root: Path) -> str:
    """Источник `origin` с меткой v0.2.0 на новом коммите; рабочая копия остаётся на старом."""
    git_run(root, "commit", "-q", "--allow-empty", "-m", "release", "--no-gpg-sign")
    git_run(root, "tag", "v0.2.0")
    origin = root.parent / "origin.git"
    git_run(root, "clone", "-q", "--bare", str(root), str(origin))
    git_run(root, "reset", "-q", "--hard", "HEAD~1")
    git_run(root, "tag", "-d", "v0.2.0")
    git_run(root, "remote", "add", "origin", str(origin))
    return git_run(root, "rev-parse", "HEAD")


def docker_failing_on(tmp_path: Path, word: str) -> None:
    """Заглушка docker: пишет argv, на вызове со словом `word` в аргументах завершается кодом 1."""
    body = 'printf "%s\\n" "$*" >> "$DOCKER_ARGV"\n'
    body += 'printf "%s\\n" "${KASPISA_VERSION-unset}" >> "$DOCKER_ARGV.versions"\n'
    body += f'case " $* " in *" {word} "*) exit 1 ;; esac\nexit 0\n'
    write_stub(tmp_path / "bin", "docker", body)


def docker_versions(tmp_path: Path) -> list[str]:
    """KASPISA_VERSION в окружении каждого вызова docker (по порядку, как в docker-argv.txt)."""
    path = tmp_path / "docker-argv.txt.versions"
    return path.read_text(encoding="utf-8").splitlines()


def test_update_rollback_in_build_mode_resets_merge(tmp_path: Path) -> None:
    """Режим сборки: слияние уже сделано, `make lint` упал - HEAD возвращён на старый коммит."""
    root = installed(tmp_path)
    old_head = with_origin_release(root)
    write_stub(
        tmp_path / "bin",
        "make",
        'case " $* " in *" lint "*) exit 1 ;; esac\nexit 0\n',
    )
    done = run_script(root, ["update.sh", "v0.2.0"])
    assert done.returncode == 1, done.stderr
    assert git_run(root, "rev-parse", "HEAD") == old_head, "слияние не отменено"
    assert git_run(root, "status", "--porcelain") == ""
    assert not (root / "backups" / ".lock.d").exists()


def test_update_build_mode_merges_remote_branch(tmp_path: Path) -> None:
    """Обновление по ветке: origin/<ветка> впереди локальной, к `make lint` HEAD уже на нём."""
    root = installed(tmp_path)
    git_run(root, "commit", "-q", "--allow-empty", "-m", "news", "--no-gpg-sign")
    branch = git_run(root, "symbolic-ref", "--short", "HEAD")
    remote_head = git_run(root, "rev-parse", "HEAD")
    origin = root.parent / "origin.git"
    git_run(root, "clone", "-q", "--bare", str(root), str(origin))
    git_run(root, "reset", "-q", "--hard", "HEAD~1")
    git_run(root, "remote", "add", "origin", str(origin))
    old_head = git_run(root, "rev-parse", "HEAD")
    assert old_head != remote_head
    seen = tmp_path / "head-at-lint.txt"
    write_stub(
        tmp_path / "bin",
        "make",
        f'case " $* " in *" lint "*) git rev-parse HEAD > "{seen}"; exit 1 ;; esac\nexit 0\n',
    )
    done = run_script(root, ["update.sh", branch])
    assert done.returncode == 1, done.stderr  # lint нарочно падает: откат
    assert seen.read_text(encoding="utf-8").strip() == remote_head, "слит не origin/ветка"
    assert git_run(root, "rev-parse", "HEAD") == old_head


def test_update_build_mode_prefers_tag_over_same_named_branch(tmp_path: Path) -> None:
    """В origin метка и ветка v0.2.0 на разных коммитах: к `make lint` слита метка, не ветка."""
    root = installed(tmp_path)
    git_run(root, "commit", "-q", "--allow-empty", "-m", "tagged", "--no-gpg-sign")
    git_run(root, "tag", "v0.2.0")
    tag_head = git_run(root, "rev-parse", "HEAD")
    git_run(root, "reset", "-q", "--hard", "HEAD~1")
    git_run(root, "commit", "-q", "--allow-empty", "-m", "branch-only", "--no-gpg-sign")
    git_run(root, "branch", "v0.2.0")
    branch_head = git_run(root, "rev-parse", "HEAD")
    assert tag_head != branch_head
    git_run(root, "reset", "-q", "--hard", "HEAD~1")
    origin = root.parent / "origin.git"
    git_run(root, "clone", "-q", "--bare", str(root), str(origin))
    git_run(root, "tag", "-d", "v0.2.0")
    git_run(root, "branch", "-D", "v0.2.0")
    git_run(root, "remote", "add", "origin", str(origin))
    old_head = git_run(root, "rev-parse", "HEAD")
    seen = tmp_path / "head-at-lint.txt"
    write_stub(
        tmp_path / "bin",
        "make",
        f'case " $* " in *" lint "*) git rev-parse HEAD > "{seen}"; exit 1 ;; esac\nexit 0\n',
    )
    done = run_script(root, ["update.sh", "v0.2.0"])
    assert done.returncode == 1, done.stderr  # lint нарочно падает: откат
    assert seen.read_text(encoding="utf-8").strip() == tag_head, "слита ветка вместо метки"
    assert git_run(root, "rev-parse", "HEAD") == old_head


@pytest.mark.parametrize(
    ("extra", "target"),
    [([], "test-backend"), (["--full-tests"], "test")],
)
def test_update_build_mode_test_target(tmp_path: Path, extra: list[str], target: str) -> None:
    """Режим сборки: по умолчанию `make test-backend`, с `--full-tests` — полный `make test`."""
    root = installed(tmp_path)
    old_head = with_origin_release(root)
    calls = tmp_path / "make-argv.txt"
    write_stub(
        tmp_path / "bin",
        "make",
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'case " $* " in *" test "*|*" test-backend "*) exit 1 ;; esac\nexit 0\n',
    )
    done = run_script(root, ["update.sh", "v0.2.0", *extra])
    assert done.returncode == 1, done.stderr  # тесты нарочно падают: откат
    targets = calls.read_text(encoding="utf-8").split()
    assert target in targets
    assert ({"test", "test-backend"} - {target}).isdisjoint(targets)
    assert git_run(root, "rev-parse", "HEAD") == old_head


def test_update_rejects_unknown_option(tmp_path: Path) -> None:
    """Неизвестный параметр после версии: отказ до слияния, HEAD прежний."""
    root = installed(tmp_path)
    old_head = with_origin_release(root)
    done = run_script(root, ["update.sh", "v0.2.0", "--fast"])
    assert done.returncode == 1, done.stderr
    assert "неизвестный параметр" in done.stdout + done.stderr
    assert git_run(root, "rev-parse", "HEAD") == old_head


@pytest.mark.parametrize("missing", TOOLS)
def test_update_build_mode_requires_tools(tmp_path: Path, missing: str) -> None:
    """Нет одного из uv, node, npm: отказ до слияния, код 1, HEAD прежний, ссылка на docs."""
    root = installed(tmp_path)
    old_head = with_origin_release(root)
    (tmp_path / "bin" / missing).unlink()
    # PATH только с заглушками и нужными системными утилитами, без uv, node и npm.
    tools_dir = tmp_path / "systools"
    tools_dir.mkdir()
    for name in ("bash", "git", "date", "sed", "tail", "mkdir", "rm", "cat", "dirname", "env",
                 "chmod", "mv", "sleep", "kill", "ls", "head", "tr", "id", "uname", "mktemp",
                 "stat", "cut", "grep", "awk", "touch", "cp", "ln", "basename", "readlink",
                 "sort", "wc"):  # fmt: skip
        found = shutil.which(name)
        if found:
            (tools_dir / name).symlink_to(found)
    path = f"{tmp_path / 'bin'}{os.pathsep}{tools_dir}"
    done = run_script(root, ["update.sh", "v0.2.0"], {"PATH": path})
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert f"не найден {missing}:" in out and "docs/deploy.md" in out
    assert git_run(root, "rev-parse", "HEAD") == old_head
    assert git_run(root, "status", "--porcelain") == ""
    assert not (root / "backups" / ".lock.d").exists()


def test_update_rollback_when_images_not_pulled(tmp_path: Path) -> None:
    """Сбой скачивания образов: код 1, HEAD, ветка и `.env` прежние, ничего не остановлено."""
    root = installed(tmp_path)
    set_registry(root)
    old_head = with_origin_release(root)
    branch = git_run(root, "symbolic-ref", "--short", "HEAD")
    env_before = (root / ".env").read_bytes()
    docker_failing_on(tmp_path, "pull")
    done = run_script(root, ["update.sh", "v0.2.0"])
    assert done.returncode == 1, done.stderr
    assert git_run(root, "rev-parse", "HEAD") == old_head
    assert git_run(root, "symbolic-ref", "--short", "HEAD") == branch, "ветка не возвращена"
    assert (root / ".env").read_bytes() == env_before
    words = [line.split() for line in docker_calls(tmp_path)]
    assert not any("stop" in w[:6] or "up" in w[:6] for w in words)
    assert not (root / "backups" / ".lock.d").exists(), "блокировка не снята"
    assert not list(root.glob(".env.tmp*"))


def test_update_rollback_after_stop_restarts_old_version(tmp_path: Path) -> None:
    """Сбой копии базы после остановки: старая версия поднята снова, git и `.env` прежние."""
    root = installed(tmp_path)
    set_registry(root)
    old_head = with_origin_release(root)
    env_before = (root / ".env").read_bytes()
    docker_failing_on(tmp_path, "exec")
    done = run_script(root, ["update.sh", "v0.2.0"])
    assert done.returncode == 1, done.stderr
    assert git_run(root, "rev-parse", "HEAD") == old_head
    assert (root / ".env").read_bytes() == env_before
    lines = docker_calls(tmp_path)
    stops = [i for i, line in enumerate(lines) if " stop " in f" {line} "]
    ups = [i for i, line in enumerate(lines) if " up " in f" {line} "]
    assert stops and ups and max(ups) > min(stops), "после остановки старая версия не запущена"
    versions = docker_versions(tmp_path)
    assert any(v == "v0.2.0" for v in versions), "новая версия не передавалась образам"
    assert versions[max(ups)] != "v0.2.0", "откат поднял новую версию"
    assert not (root / "backups" / ".lock.d").exists(), "блокировка не снята"
    log = next((root / "backups").glob("update-*.log")).read_text(encoding="utf-8")
    assert "прежняя версия" in log + done.stderr
