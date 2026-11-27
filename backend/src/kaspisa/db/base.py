"""Основа моделей: соглашение об именах ограничений и тип идентификатора арендатора."""

from __future__ import annotations

from typing import NewType
from uuid import UUID

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Идентификатор арендатора отличается от идентификатора магазина для mypy.
TenantId = NewType("TenantId", UUID)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Значения по умолчанию из базы читаются тем же INSERT/UPDATE (RETURNING):
    # ленивая подгрузка в асинхронной сессии невозможна.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012
