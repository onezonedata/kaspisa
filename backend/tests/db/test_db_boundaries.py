"""Т30: границы пакетов и отсутствие выдачи сессии наружу."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from db_seed import TwoTenants

import kaspisa
import kaspisa.db as db_package
from kaspisa.db import Database, SystemScope, TenantScope

SRC = Path(kaspisa.__file__).resolve().parent
FORBIDDEN_OUTSIDE_DB = {"sqlalchemy", "asyncpg"}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _modules_outside(package: str) -> list[Path]:
    return [p for p in SRC.rglob("*.py") if package not in p.relative_to(SRC).parts[:1]]


def test_sqlalchemy_only_inside_db() -> None:
    for path in _modules_outside("db"):
        roots = {name.split(".")[0] for name in _imports(path)}
        assert not roots & FORBIDDEN_OUTSIDE_DB, path


def test_security_does_not_import_db() -> None:
    for path in (SRC / "security").rglob("*.py"):
        assert not any(n.startswith("kaspisa.db") for n in _imports(path)), path


def test_pricing_imports_nothing_from_project() -> None:
    for path in (SRC / "pricing").rglob("*.py"):
        foreign = [
            n
            for n in _imports(path)
            if n.split(".")[0] == "kaspisa" and not n.startswith("kaspisa.pricing")
        ]
        assert not foreign, path


def test_public_names_are_not_sqlalchemy_objects() -> None:
    for name in db_package.__all__:
        obj = getattr(db_package, name)
        module = getattr(obj, "__module__", "") or ""
        assert not module.startswith(("sqlalchemy", "asyncpg")), name


def _public(cls: type) -> list[str]:
    return [n for n in dir(cls) if not n.startswith("_")]


def test_no_public_session_annotations() -> None:
    for cls in (Database, TenantScope, SystemScope):
        for name in _public(cls):
            member = getattr(cls, name)
            if not callable(member):
                continue
            ret = str(inspect.signature(member).return_annotation)
            assert not any(w in ret for w in ("Session", "Engine", "Connection")), (cls, name)
        for name in _public(cls):
            assert "session" not in name.lower() or name == "commit", (cls, name)
            assert "engine" not in name.lower() and "connection" not in name.lower(), (cls, name)


async def test_live_scope_has_no_public_session_values(
    database: Database, two_tenants: TwoTenants
) -> None:
    async with database.tenant(two_tenants.a.tenant_id) as scope:
        for name in _public(TenantScope):
            value = getattr(scope, name)
            module = type(value).__module__
            assert not module.startswith(("sqlalchemy", "asyncpg")), name
    async with database.system() as system:
        for name in _public(SystemScope):
            module = type(getattr(system, name)).__module__
            assert not module.startswith(("sqlalchemy", "asyncpg")), name
    for name in _public(Database):
        module = type(getattr(database, name)).__module__
        assert not module.startswith(("sqlalchemy", "asyncpg")), name
