"""Т14: границы пакета worker (разбор исходников без запуска)."""

from __future__ import annotations

import ast
from pathlib import Path

import kaspisa

WORKER = Path(kaspisa.__file__).resolve().parent / "worker"
FORBIDDEN = ("sqlalchemy", "asyncpg", "kaspisa.db.models")


def _modules() -> list[Path]:
    return sorted(WORKER.rglob("*.py"))


def test_worker_does_not_import_database_internals() -> None:
    modules = _modules()
    assert modules
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                assert not name.startswith(FORBIDDEN), (path.name, name)


def test_set_price_is_named_only_in_writer() -> None:
    for path in _modules():
        if path.name == "writer.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr != "set_price", path.name
            if isinstance(node, ast.Name):
                assert node.id != "set_price", path.name
