"""Помощники тестов скрипта выпуска зеркала (ADR 0028, этап 16).

Временный репозиторий-источник строится из рабочего набора файлов, зеркало — пустой git-репозиторий,
сканер и docker подменены заглушками в папке `bin` (ничего не публикуется, сеть не нужна).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from deploy_testing import REPO

GIT = shutil.which("git")
BASH = shutil.which("bash")
assert GIT is not None
assert BASH is not None

SCRIPT_NAME = "build-mirror.sh"
# Закрытые пути не копируются в источник: скрипт их всё равно не экспортирует.
SKIP_PREFIX = "docs/" + "stages/"
SRC_CONFIG = (
    "-c",
    "user.name=Src",
    "-c",
    "user.email=src@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)
# Инструменты, нужные скрипту: PATH заглушек состоит только из них (docker и gitleaks — заглушки).
TOOLS = (
    "awk",
    "basename",
    "bash",
    "cat",
    "cp",
    "dirname",
    "find",
    "grep",
    "head",
    "ls",
    "mkdir",
    "mktemp",
    "rm",
    "sed",
    "sort",
    "tar",
    "tr",
    "wc",
)


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Запускает git в `cwd` с настройками без хуков и подписи; возвращает stdout."""
    assert GIT is not None
    done = subprocess.run(  # noqa: S603
        [GIT, *SRC_CONFIG, "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if check:
        assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def working_set(repo: Path) -> list[str]:
    """Файлы рабочего набора: отслеживаемые и неотслеживаемые неигнорируемые, без удалённых."""
    assert GIT is not None
    raw = subprocess.run(  # noqa: S603
        [GIT, "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    ).stdout
    names = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    return sorted({n for n in names if os.path.lexists(repo / n)})


def build_source(dest: Path) -> Path:
    """Временный репозиторий-источник из рабочего набора закрытого репозитория."""
    dest.mkdir(parents=True)
    for name in working_set(REPO):
        if name.startswith(SKIP_PREFIX):
            continue
        origin = REPO / name
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_symlink():
            os.symlink(os.readlink(origin), target)
        elif origin.is_file():
            shutil.copy2(origin, target)
    git(dest, "init", "-q", "-b", "main")
    commit_all(dest, "источник")
    return dest


def clone_source(src: Path, dest: Path) -> Path:
    """Копия источника: свой `.git`, чтобы правки не трогали общий источник."""
    git(src.parent, "clone", "-q", str(src), str(dest))
    return dest


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", message)


def make_mirror(path: Path, *, email: bool = True) -> Path:
    """Пустой git-репозиторий зеркала с локальным автором."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "--local", "user.name", "Mirror Test")
    if email:
        git(path, "config", "--local", "user.email", "mirror@example.invalid")
    return path


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False)


def snapshot(root: Path) -> dict[str, str]:
    """Список каталогов и файлов с хешами; `.git` верхнего уровня не входит, вложенные входят."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root and ".git" in dirnames:
            dirnames.remove(".git")
        out[f"d:{here.relative_to(root)}"] = ""
        for name in filenames:
            path = here / name
            rel = f"f:{path.relative_to(root)}"
            if path.is_symlink():
                out[rel] = "link:" + os.readlink(path)
            else:
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def tree_files(root: Path) -> dict[str, bytes]:
    """Файлы дерева (без `.git`) по относительным путям."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@dataclass
class Rig:
    """Окружение запуска: папка заглушек, пустой TMPDIR, журналы и маркеры заглушек."""

    base: Path

    @property
    def bin(self) -> Path:
        return self.base / "bin"

    @property
    def tmp(self) -> Path:
        return self.base / "tmp"

    @property
    def home(self) -> Path:
        return self.base / "home"

    @property
    def marks(self) -> Path:
        return self.base / "marks"

    @property
    def snap(self) -> Path:
        return self.base / "snap"

    @property
    def gitleaks_log(self) -> Path:
        return self.base / "gitleaks.log"

    @property
    def docker_log(self) -> Path:
        return self.base / "docker.log"

    def env(self) -> dict[str, str]:
        return {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "leak",
            "GIT_AUTHOR_EMAIL": "leak@example.invalid",
            "GIT_COMMITTER_NAME": "leak",
            "GIT_COMMITTER_EMAIL": "leak@example.invalid",
            "EMAIL": "leak@example.invalid",
            # Настройки сканера из окружения владельца: скрипт обязан их снять.
            "GITLEAKS_CONFIG": "/nonexistent/x.toml",
            "GITLEAKS_CONFIG_TOML": "x",
        }


def make_rig(
    base: Path,
    *,
    gitleaks: str | None = "v8.21.2",
    gitleaks_code: int = 0,
    docker: str | int = "marker",
) -> Rig:
    """Папка заглушек.

    `gitleaks` — версия заглушки или None (нет). `docker`: "marker" (пишет маркер, код 1),
    "absent" (нет) или число — заглушка пишет журнал и выходит с этим кодом.
    """
    rig = Rig(base)
    for folder in (rig.bin, rig.tmp, rig.home, rig.marks, rig.snap):
        folder.mkdir(parents=True)
    for name in TOOLS:
        found = shutil.which(name)
        assert found is not None, name
        (rig.bin / name).symlink_to(found)
    assert GIT is not None
    _write(rig.bin / "git", f'#!/bin/sh\nexec {shlex.quote(GIT)} "$@"\n')
    gh_marker = shlex.quote(str(rig.marks / "gh"))
    _write(rig.bin / "gh", f"#!/bin/sh\necho called > {gh_marker}\nexit 1\n")
    if gitleaks is not None:
        _write(rig.bin / "gitleaks", _scanner_stub(rig, "gitleaks", gitleaks, gitleaks_code))
    if docker == "marker":
        marker = shlex.quote(str(rig.marks / "docker"))
        _write(rig.bin / "docker", f"#!/bin/sh\necho called > {marker}\nexit 1\n")
    elif isinstance(docker, int):
        _write(rig.bin / "docker", _scanner_stub(rig, "docker", None, docker))
    return rig


def _scanner_stub(rig: Rig, kind: str, version: str | None, code: int) -> str:
    """Заглушка gitleaks (version/dir) или docker (run ... -v S:S:ro IMAGE dir ... S)."""
    log = shlex.quote(str(rig.base / f"{kind}.log"))
    snap = shlex.quote(str(rig.snap))
    lines = ["#!/bin/sh"]
    if version is not None:
        lines += [f'if [ "$1" = version ]; then echo {shlex.quote(version)}; exit 0; fi']
    lines += [
        f"echo '---' >> {log}",
        f'for a in "$@"; do printf \'%s\\n\' "$a" >> {log}; done',
        'last=""',
        'for a in "$@"; do last="$a"; done',
        f'if [ -e "$last/public-root" ]; then echo present > {snap}/public_root;'
        f" else echo absent > {snap}/public_root; fi",
        f'cp "$last/AGENTS.md" {snap}/AGENTS.md 2>/dev/null',
        f'if [ -n "${{GITLEAKS_CONFIG+x}}" ]; then echo set > {snap}/config_env;'
        f" else echo unset > {snap}/config_env; fi",
        f'if [ -n "${{GITLEAKS_CONFIG_TOML+x}}" ]; then echo set > {snap}/config_toml_env;'
        f" else echo unset > {snap}/config_toml_env; fi",
        f"exit {code}",
    ]
    return "\n".join(lines) + "\n"


def calls(log: Path) -> list[list[str]]:
    """Вызовы заглушки: список списков аргументов."""
    if not log.exists():
        return []
    chunks = log.read_text(encoding="utf-8").split("---\n")[1:]
    return [chunk.splitlines() for chunk in chunks]


def run_build(src: Path, target: Path | str, rig: Rig) -> subprocess.CompletedProcess[str]:
    """Запуск `release/build-mirror.sh` из репозитория-источника."""
    assert BASH is not None
    return subprocess.run(  # noqa: S603
        [BASH, str(src / "release" / SCRIPT_NAME), str(target)],
        env=rig.env(),
        cwd=rig.home,
        capture_output=True,
        text=True,
        check=False,
        timeout=100,
    )


def output(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout + done.stderr
