"""Т3, Т5: `register_first` под advisory-блокировкой (ADR 0027)."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path

from sqlalchemy import NullPool, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kaspisa.db import Database, User
from kaspisa.db.system import FIRST_ACCOUNT_LOCK

POSTGRES_DIR = Path(__file__).resolve().parents[3] / "deploy" / "postgres"
HASH = "scrypt$stub"


async def _first(database: Database, email: str) -> User | None:
    async with database.system() as system:
        return await system.register_first(email=email, password_hash=HASH)


async def _scalar(url: str, sql: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            return int((await conn.execute(text(sql))).scalar_one())
    finally:
        await engine.dispose()


async def test_register_first_serializes_under_lock(
    database_url: str, committed_database: Database
) -> None:
    """Т3: пять вызовов ждут блокировку; пользователя создаёт ровно один."""
    holder = create_async_engine(
        database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    waiting_sql = (
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    try:
        async with holder.connect() as conn:
            await conn.execute(
                text("SELECT pg_advisory_lock(CAST(:k AS bigint))"), {"k": FIRST_ACCOUNT_LOCK}
            )
            tasks = [
                asyncio.create_task(_first(committed_database, f"u{i}@example.com"))
                for i in range(5)
            ]
            deadline = time.monotonic() + 10
            waiting = 0
            while time.monotonic() < deadline:
                waiting = await _scalar(database_url, waiting_sql)
                if waiting == 5:
                    break
                await asyncio.sleep(0.05)
            assert waiting == 5
            await conn.execute(
                text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": FIRST_ACCOUNT_LOCK}
            )
            results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
    finally:
        await holder.dispose()

    assert sum(1 for r in results if r is not None) == 1
    assert sum(1 for r in results if r is None) == 4
    assert await _scalar(database_url, "SELECT count(*) FROM app_user") == 1
    assert await _scalar(database_url, "SELECT count(*) FROM tenant") == 1


async def test_register_first_as_app_role(database_url: str, committed_database: Database) -> None:
    """Т5: роль с атрибутами `kaspisa_app` вправе брать блокировку и создавать пользователя."""
    role = f"kaspisa_app_t5_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(
        database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_role(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute(f'SET ROLE "{role}"')
        cursor.close()

    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE ROLE "{role}" NOSUPERUSER NOCREATEDB NOCREATEROLE'))
            await conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
                    f'TO "{role}"'
                )
            )
            await conn.execute(
                text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"')
            )
        database = Database(
            async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
            engine=engine,
        )
        async with engine.connect() as conn:  # движок и правда работает под ролью теста
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == role
        user = await _first(database, "owner@example.com")
        assert user is not None
        assert user.email == "owner@example.com"
        assert await _first(database, "other@example.com") is None
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin.dispose()


def test_deploy_sql_does_not_revoke_function_execute() -> None:
    """Т5, статический пункт: роли не отзывают EXECUTE на встроенные функции."""
    files = sorted(POSTGRES_DIR.glob("*.sql"))
    assert files
    for path in files:
        for statement in re.findall(r"(?is)\brevoke\b[^;]*;", path.read_text()):
            assert not re.search(r"(?i)\b(function|functions|routine|routines)\b", statement), (
                path.name
            )


async def test_register_first_normalizes_email_in_data_layer(
    database_url: str, committed_database: Database
) -> None:
    """Слой данных сам приводит e-mail к нижнему регистру: и при создании, и при поиске."""
    user = await _first(committed_database, "Owner@Example.COM")
    assert user is not None
    assert user.email == "owner@example.com"
    async with committed_database.system() as system:
        found = await system.find_user_by_email("OWNER@example.com")
    assert found is not None
    assert found.id == user.id
    named = "SELECT count(*) FROM tenant WHERE name = 'owner@example.com'"
    assert await _scalar(database_url, named) == 1
