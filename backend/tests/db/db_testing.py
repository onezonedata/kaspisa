"""Вспомогательные функции тестовой базы: имена, создание и удаление баз прогона."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

from alembic.config import Config
from sqlalchemy import NullPool, text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic" / "alembic.ini"


def alembic_config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    return config


_NAME_RE = re.compile(r"^kaspisa_test_[0-9a-f]{8}(_m)?$")


def server_params() -> tuple[str, int, str, str]:
    """Адрес сервера тестов: хост, порт, пользователь, пароль."""
    port = int(os.environ.get("KASPISA_PG_PORT", "5432"))
    return "localhost", port, "kaspisa", "kaspisa"


def new_test_db_name() -> str:
    return f"kaspisa_test_{uuid.uuid4().hex[:8]}"


def is_test_db_name(name: str) -> bool:
    return _NAME_RE.fullmatch(name) is not None


def sqlalchemy_url(db_name: str) -> str:
    host, port, user, password = server_params()
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"


def _unavailable(exc: BaseException) -> RuntimeError:
    host, port, user, _ = server_params()
    return RuntimeError(
        f"Postgres недоступен по адресу {user}:***@{host}:{port}: выполните make up "
        f"(порт задаётся KASPISA_PG_PORT); причина: {type(exc).__name__}"
    )


async def _admin_execute(sql: str) -> None:
    """Выполнить служебную команду в базе postgres без транзакции."""
    engine = create_async_engine(
        sqlalchemy_url("postgres"),
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    except (OSError, OperationalError, InterfaceError) as exc:
        raise _unavailable(exc) from None
    finally:
        await engine.dispose()


async def create_test_database(name: str) -> None:
    """Создать базу прогона. Имя обязано подходить под шаблон."""
    if not is_test_db_name(name):
        raise ValueError(f"недопустимое имя тестовой базы: {name!r}")
    await _admin_execute(f'CREATE DATABASE "{name}"')


async def drop_test_database(name: str) -> None:
    """Удалить базу прогона. Отказывается от имён не по шаблону."""
    if not is_test_db_name(name):
        raise ValueError(f"отказ удалять базу с недопустимым именем: {name!r}")
    await _admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def create_test_database_sync(name: str) -> None:
    asyncio.run(create_test_database(name))


def drop_test_database_sync(name: str) -> None:
    asyncio.run(drop_test_database(name))
