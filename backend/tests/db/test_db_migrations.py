"""Т39, Т25, Т26: имена тестовых баз, миграция вверх и вниз, расхождение моделей."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    is_test_db_name,
    new_test_db_name,
    sqlalchemy_url,
)
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

TABLES = {
    "tenant",
    "app_user",
    "shop",
    "product",
    "price_change",
    "price_write",
    "owner_event",
    "telegram_settings",
}


def test_db_name_guard() -> None:
    for bad in ("kaspisa", "postgres", "kaspisa_test", "kaspisa_test_xyz", "kaspisa_test_0A1B2C3D"):
        assert not is_test_db_name(bad)
    assert is_test_db_name("kaspisa_test_0a1b2c3d")
    assert is_test_db_name("kaspisa_test_0a1b2c3d_m")
    assert new_test_db_name() != new_test_db_name()
    assert is_test_db_name(new_test_db_name())


def test_drop_refuses_foreign_names() -> None:
    for bad in ("kaspisa", "postgres", "kaspisa_test_xyz"):
        with pytest.raises(ValueError, match="недопустимым именем"):
            drop_test_database_sync(bad)


def test_unavailable_postgres_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сервер недоступен: понятная ошибка без пароля, а не пропуск тестов."""
    monkeypatch.setenv("KASPISA_PG_PORT", "1")
    with pytest.raises(RuntimeError) as info:
        create_test_database_sync(new_test_db_name())
    text_ = str(info.value)
    assert "Postgres недоступен" in text_
    assert "make up" in text_
    assert ":***@" in text_
    assert "kaspisa:kaspisa" not in text_


async def _scalars(url: str, sql: str) -> Sequence[str]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            return [row[0] for row in (await conn.execute(text(sql)))]
    finally:
        await engine.dispose()


def _tables(url: str) -> set[str]:
    sql = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    return set(asyncio.run(_scalars(url, sql)))


def _leftovers(url: str) -> dict[str, Sequence[str]]:
    queries = {
        "types": "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'c') "
        "AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.reltype = t.oid)",
        "sequences": "SELECT relname FROM pg_class WHERE relkind = 'S' "
        "AND relnamespace = 'public'::regnamespace",
        "functions": "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace",
        "triggers": "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal",
    }
    return {name: asyncio.run(_scalars(url, sql)) for name, sql in queries.items()}


def test_upgrade_downgrade_upgrade_on_clean_database() -> None:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "head")
        assert _tables(url) >= TABLES
        assert "forbid_tenant_change" in _leftovers(url)["functions"]
        # и `owner_event` (ADR 0024), `telegram_settings` (ADR 0025)
        assert len(_leftovers(url)["triggers"]) == 7

        command.downgrade(config, "base")
        assert _tables(url) == {"alembic_version"}
        assert _leftovers(url) == {"types": [], "sequences": [], "functions": [], "triggers": []}

        command.upgrade(config, "head")
        assert _tables(url) >= TABLES
    finally:
        drop_test_database_sync(name)


def test_alembic_check_finds_no_drift(database_url: str) -> None:
    command.check(alembic_config(database_url))
