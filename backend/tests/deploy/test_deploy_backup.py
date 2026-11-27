"""Т35–Т37: `deploy/backup.sh` — копия, ротация, расписание. Блокировку проверяет приёмка (Т61)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from deploy_testing import REPO, write_stub

BACKUP_SH = REPO / "deploy" / "backup.sh"
TS = "%Y%m%dT%H%M%SZ"
BASH = shutil.which("bash")

# Заглушка flock: в macOS его нет. Пропускает параметры и выполняет команду.
FLOCK = """\
while [ $# -gt 0 ]; do
    case "$1" in
    -n) shift ;;
    -w | -E) shift 2 ;;
    *) break ;;
    esac
done
shift
exec "$@"
"""
PG_DUMP = """\
echo x >> "$STUB_LOG"
echo PARTIAL
[ -n "${STUB_PG_DUMP_RC:-}" ] && exit "$STUB_PG_DUMP_RC"
echo DUMP
"""


class Box:
    def __init__(self, root: Path) -> None:
        self.dir = root / "backups"
        self.dir.mkdir()
        self.log = root / "pg_dump.log"
        bindir = root / "bin"
        bindir.mkdir()
        write_stub(bindir, "flock", FLOCK)
        write_stub(bindir, "pg_dump", PG_DUMP)
        write_stub(bindir, "psql", 'echo psql >> "$STUB_LOG"\n')
        write_stub(bindir, "pg_restore", 'echo pg_restore >> "$STUB_LOG"\n')
        self.path = f"{bindir}{os.pathsep}{os.environ['PATH']}"

    def put(self, klass: str, age: timedelta) -> Path:
        name = f"{klass}-{(datetime.now(UTC) - age).strftime(TS)}.dump"
        path = self.dir / name
        path.write_text("OLD", encoding="utf-8")
        return path

    def run(self, *args: str, pg_dump_rc: str = "") -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        env = {
            "PATH": self.path,
            "KASPISA_BACKUP_DIR": str(self.dir),
            "STUB_LOG": str(self.log),
            "STUB_PG_DUMP_RC": pg_dump_rc,
        }
        return subprocess.run(  # noqa: S603
            [BASH, str(BACKUP_SH), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def names(self, prefix: str) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir() if p.name.startswith(prefix))

    def dump_calls(self) -> int:
        return len(self.log.read_text().split()) if self.log.exists() else 0


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def test_dump_daily_rotates_and_sets_mode(box: Box) -> None:
    """Т35: из 8 суточных остаётся 7 с новой; ручные и посторонние файлы целы; права 600."""
    old = [box.put("daily", timedelta(days=d)) for d in range(8, 0, -1)]
    manual = [box.put("manual", timedelta(days=1)), box.put("manual", timedelta(days=2))]
    stranger = box.dir / "daily-notes.txt"
    stranger.write_text("notes", encoding="utf-8")
    done = box.run("dump", "daily")
    assert done.returncode == 0, done.stderr
    daily = box.names("daily-")
    assert len([n for n in daily if n.endswith(".dump")]) == 7
    assert old[0].name not in daily and old[1].name not in daily
    assert all(p.name in daily for p in old[2:])
    new = [n for n in daily if n.endswith(".dump") and n not in {p.name for p in old}]
    assert len(new) == 1
    assert stat.S_IMODE((box.dir / new[0]).stat().st_mode) == 0o600
    assert (box.dir / new[0]).read_text().strip().endswith("DUMP")
    assert all(p.exists() for p in manual)
    assert stranger.exists()
    assert not list(box.dir.glob("*.tmp")) and not list(box.dir.glob(".*.tmp"))


def test_dump_failure_keeps_old_files(box: Box) -> None:
    """Т36: сбой pg_dump — код 1, новых файлов нет, старые на месте, временных нет."""
    old = [box.put("daily", timedelta(days=d)) for d in range(7, 0, -1)]
    before = sorted(p.name for p in box.dir.iterdir())
    done = box.run("dump", "daily", pg_dump_rc="1")
    assert done.returncode == 1
    assert sorted(p.name for p in box.dir.iterdir()) == before
    assert all(p.exists() for p in old)


def test_daily_if_due(box: Box) -> None:
    """Т37: суточная копия делается, если свежей нет или ей 24 часа и больше."""
    fresh = box.put("daily", timedelta(hours=23))
    assert box.run("daily-if-due").returncode == 0
    assert box.dump_calls() == 0
    fresh.unlink()

    box.put("daily", timedelta(hours=25))
    assert box.run("daily-if-due").returncode == 0
    assert box.dump_calls() == 1
    assert len(box.names("daily-")) == 2


def test_daily_if_due_without_copies(box: Box) -> None:
    """Т37: копий нет — копия делается."""
    assert box.run("daily-if-due").returncode == 0
    assert box.dump_calls() == 1
    assert len(box.names("daily-")) == 1


def test_manual_keeps_three(box: Box) -> None:
    """Т37: четыре ручные копии подряд — остаётся три (одинаковые секунды не затирают файл)."""
    for _ in range(4):
        assert box.run("manual").returncode == 0
    assert len(box.names("manual-")) == 3


@pytest.mark.parametrize("name", ["../x.dump", "/etc/passwd", "/backups/../x.dump", "x.dump"])
def test_restore_rejects_bad_names(box: Box, name: str) -> None:
    """Имя копии — только `<каталог>/(daily|manual)-<метка>.dump`; psql и pg_restore молчат."""
    done = box.run("restore", name)
    assert done.returncode == 2
    assert not box.log.exists()
