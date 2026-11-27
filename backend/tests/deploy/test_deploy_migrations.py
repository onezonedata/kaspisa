"""Т28: миграции не требуют суперпользователя (роль владельца базы — без SUPERUSER)."""

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

FORBIDDEN = [
    r"CREATE\s+EXTENSION",
    r"ALTER\s+SYSTEM",
    r"TO\s+PROGRAM",
    r"OWNER\s+TO",
    r"CREATE\s+ROLE",
    r"ALTER\s+ROLE",
]


def test_migrations_do_not_need_superuser() -> None:
    files = sorted(VERSIONS.glob("*.py"))
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            assert not re.search(pattern, text, re.IGNORECASE), f"{path.name}: {pattern}"
