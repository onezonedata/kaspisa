"""Т1-Т4 этапа 24: миграция 0011 и системные методы уведомления о сбросе остатка (ADR 0038)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from db_worker_seed import insert_product, insert_shop, insert_tenant
from sqlalchemy import NullPool, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command
from kaspisa.db import Database, TenantId

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


@contextmanager
def _database_at_0010() -> Iterator[tuple[str, Any]]:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0010")
        yield url, config
    finally:
        drop_test_database_sync(name)


def _run(url: str, work: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _columns(conn: AsyncConnection) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'product' AND column_name LIKE 'stock_reset%'"
        )
    )
    return {row[0] for row in result}


def test_migration_0011_adds_columns_and_rolls_back() -> None:
    """Т1 (и Т2): столбцы не заполняются, откат проходит, повторный подъём тоже."""
    with _database_at_0010() as (url, config):

        async def seed(conn: AsyncConnection) -> int:
            tenant = await insert_tenant(conn)
            shop = await insert_shop(conn, tenant, state="connected", merchant_uid="30001")
            return await insert_product(conn, tenant, shop, tracked=False)

        product_id = _run(url, seed)
        command.upgrade(config, "0011")

        async def after_upgrade(conn: AsyncConnection) -> Any:
            assert await _columns(conn) == {"stock_reset_at", "stock_reset_points"}
            row = await conn.execute(
                text("SELECT stock_reset_at, stock_reset_points FROM product WHERE id = :i"),
                {"i": product_id},
            )
            await conn.execute(
                text(
                    "UPDATE product SET stock_reset_at = now(), stock_reset_points = 'PP1' "
                    "WHERE id = :i"
                ),
                {"i": product_id},
            )
            return tuple(row.one())

        assert _run(url, after_upgrade) == (None, None)
        command.downgrade(config, "0010")
        assert _run(url, _columns) == set()
        command.upgrade(config, "0011")
        assert len(_run(url, _columns)) == 2


async def test_old_untracked_products_are_not_notified(
    committed_database: Database, database_url: str
) -> None:
    """Т2: выключенный товар без отметки сброса сообщения не даёт."""
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            tenant = await insert_tenant(conn)
            shop = await insert_shop(conn, tenant, state="connected", merchant_uid="30001")
            await insert_product(conn, tenant, shop, tracked=False)
    finally:
        await engine.dispose()
    async with committed_database.system() as system:
        assert await system.pending_stock_reset_notices() == []


async def _seed_two_tenants(
    conn: AsyncConnection,
) -> tuple[uuid.UUID, uuid.UUID, int, int, int]:
    ta = await insert_tenant(conn)
    tb = await insert_tenant(conn)
    sa = await insert_shop(conn, ta, state="connected", merchant_uid="30001")
    sb = await insert_shop(conn, tb, state="connected", merchant_uid="30002")
    a1 = await insert_product(conn, ta, sa, tracked=False)
    a2 = await insert_product(conn, ta, sa, tracked=False)
    b1 = await insert_product(conn, tb, sb, tracked=False)
    return ta, tb, a1, a2, b1


async def _set_reset(conn: AsyncConnection, product_id: int, at: datetime, points: str) -> None:
    await conn.execute(
        text("UPDATE product SET stock_reset_at = :a, stock_reset_points = :p WHERE id = :i"),
        {"a": at, "p": points, "i": product_id},
    )


async def test_reset_time_requires_points(connection: AsyncConnection) -> None:
    """Т3: время без точек и точки без времени нарушают ограничение."""
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, state="connected", merchant_uid="30001")
    pid = await insert_product(connection, tenant, shop, tracked=False)
    for sql in (
        "UPDATE product SET stock_reset_at = now() WHERE id = :i",
        "UPDATE product SET stock_reset_points = 'PP1' WHERE id = :i",
    ):
        nested = await connection.begin_nested()
        with pytest.raises(IntegrityError) as info:
            await connection.execute(text(sql), {"i": pid})
        assert "stock_reset_with_points" in str(info.value)
        await nested.rollback()


async def test_pending_and_mark_stock_reset(
    committed_database: Database, database_url: str
) -> None:
    """Т4: по одной самой ранней строке на арендатора; отметка условна и фильтрует арендатора."""
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            ta, tb, a1, a2, b1 = await _seed_two_tenants(conn)
            await _set_reset(conn, a1, T0, "PP1,PP2")
            await _set_reset(conn, a2, T0 + timedelta(seconds=1), "PP3")
            await _set_reset(conn, b1, T0 + timedelta(seconds=2), "PP4")
        async with committed_database.system() as system:
            found = await system.pending_stock_reset_notices()
            assert {(n.product_id, n.points, n.reset_at) for n in found} == {
                (a1, ("PP1", "PP2"), T0),
                (b1, ("PP4",), T0 + timedelta(seconds=2)),
            }
            assert {n.title for n in found} == {"Товар"}
            assert await system.mark_stock_reset_notified(TenantId(tb), a1, T0) is False
            assert (
                await system.mark_stock_reset_notified(TenantId(ta), a1, T0 + timedelta(seconds=5))
                is False
            )
            await system.commit()
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT stock_reset_at, stock_reset_points FROM product WHERE id = :i"),
                {"i": a1},
            )
            assert tuple(row.one()) == (T0, "PP1,PP2")
        async with committed_database.system() as system:
            assert await system.mark_stock_reset_notified(TenantId(ta), a1, T0) is True
            await system.commit()
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT stock_reset_at, stock_reset_points FROM product WHERE id = :i"),
                {"i": a1},
            )
            assert tuple(row.one()) == (None, None)
        async with committed_database.system() as system:
            found = await system.pending_stock_reset_notices()
            assert {n.product_id for n in found} == {a2, b1}
            assert await system.mark_stock_reset_notified(TenantId(ta), a1, T0) is False
    finally:
        await engine.dispose()


async def test_same_reset_time_picks_lowest_id_and_mark_touches_one_product(
    committed_database: Database, database_url: str
) -> None:
    """Одно время сброса у двух товаров арендатора: берётся меньший id, отметка снимает один."""
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            ta, _, a1, a2, _ = await _seed_two_tenants(conn)
            await _set_reset(conn, a1, T0, "PP1")
            await _set_reset(conn, a2, T0, "PP2")
        async with committed_database.system() as system:
            found = await system.pending_stock_reset_notices()
            assert [n.product_id for n in found if n.tenant_id == ta] == [min(a1, a2)]
            assert await system.mark_stock_reset_notified(TenantId(ta), min(a1, a2), T0)
            await system.commit()
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT stock_reset_points FROM product WHERE id = :i"), {"i": max(a1, a2)}
            )
            assert row.scalar_one() == "PP2"
    finally:
        await engine.dispose()
