"""Фикстуры тестов базы: у каждого прогона своя база, схема создаётся миграцией."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from db_seed import TwoTenants, seed_tenant
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from kaspisa.db import Database, create_database
from kaspisa.security import KeyRing, SecretCipher


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """База прогона с миграциями до head; в конце удаляется."""
    name = new_test_db_name()
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        drop_test_database_sync(name)


@pytest.fixture
async def connection(database_url: str) -> AsyncIterator[AsyncConnection]:
    """Одно соединение с внешней транзакцией; в конце теста откат."""
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            outer = await conn.begin()
            try:
                yield conn
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def keyring() -> KeyRing:
    """Связка со случайным ключом."""
    return KeyRing(1, {1: os.urandom(32)})


@pytest.fixture
def cipher(keyring: KeyRing) -> SecretCipher:
    return SecretCipher(keyring)


@pytest.fixture
async def database(connection: AsyncConnection, cipher: SecretCipher) -> Database:
    """Database на соединении теста: commit области фиксирует точку сохранения."""
    factory = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    return Database(factory, cipher=cipher)


@pytest.fixture
async def committed_database(database_url: str, cipher: SecretCipher) -> AsyncIterator[Database]:
    """Database с настоящими commit; в конце удаляет созданных арендаторов (каскад)."""
    db = create_database(database_url, cipher=cipher)
    try:
        yield db
    finally:
        await db.dispose()
        engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM tenant"))
        finally:
            await engine.dispose()


@pytest.fixture
async def two_tenants(database: Database) -> TwoTenants:
    return TwoTenants(a=await seed_tenant(database, "a"), b=await seed_tenant(database, "b"))
