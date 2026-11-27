"""Пакет kaspisa.pricing не импортирует сеть, базу и другие пакеты проекта."""

from __future__ import annotations

import ast
from pathlib import Path

import kaspisa.pricing

ALLOWED = {"__future__", "dataclasses", "enum", "collections.abc", "kaspisa.pricing"}


def test_pricing_imports_only_allowed_modules() -> None:
    package_dir = Path(kaspisa.pricing.__file__).parent
    files = list(package_dir.glob("*.py"))
    assert files
    bad: list[str] = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name not in ALLOWED and not name.startswith("kaspisa.pricing."):
                    bad.append(f"{path.name}: {name}")
    assert not bad


def test_pricing_has_no_float_arithmetic() -> None:
    """Цены — целые тенге: в пакете нет float, деления и round."""
    package_dir = Path(kaspisa.pricing.__file__).parent
    bad: list[str] = []
    for path in package_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                bad.append(f"{path.name}:{node.lineno}: литерал float")
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                bad.append(f"{path.name}:{node.lineno}: деление /")
            elif isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Div):
                bad.append(f"{path.name}:{node.lineno}: деление /=")
            elif isinstance(node, ast.Name) and node.id in {"float", "round", "Decimal"}:
                bad.append(f"{path.name}:{node.lineno}: {node.id}")
    assert not bad, bad
