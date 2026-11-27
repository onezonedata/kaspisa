"""Окружение Alembic (асинхронный шаблон, драйвер asyncpg)."""

import asyncio

from sqlalchemy import NullPool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from kaspisa.config import Settings
from kaspisa.db import models  # noqa: F401  -- регистрирует таблицы в Base.metadata
from kaspisa.db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # Адрес, заданный вызывающим (тесты), главнее настроек окружения.
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
