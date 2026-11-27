"""Т10-Т14 этапа 20: миграция 0010 удаляет магазины без `merchant_uid` (ADR 0029)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from db_worker_seed import (
    insert_change,
    insert_product,
    insert_shop,
    insert_tenant,
    insert_write,
)
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command


@contextmanager
def _database_at_0009() -> Iterator[tuple[str, Any]]:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0009")
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


async def _shop_ids(conn: AsyncConnection) -> set[uuid.UUID]:
    return {row[0] for row in await conn.execute(text("SELECT id FROM shop"))}


async def _rows(conn: AsyncConnection, table: str, shops: list[uuid.UUID]) -> list[dict[str, Any]]:
    result = await conn.execute(
        text(f"SELECT * FROM {table} WHERE shop_id = ANY(:s) ORDER BY id"),
        {"s": shops},
    )
    return [dict(row) for row in result.mappings()]


async def _tenant_counts(conn: AsyncConnection) -> dict[uuid.UUID, int]:
    result = await conn.execute(text("SELECT tenant_id, count(*) FROM shop GROUP BY tenant_id"))
    return {row[0]: row[1] for row in result}


def test_removes_every_shop_without_merchant_uid() -> None:
    """Т10: not_connected, awaiting_code с сессией и reauth_required без uid удаляются."""
    with _database_at_0009() as (url, config):

        async def seed(conn: AsyncConnection) -> list[uuid.UUID]:
            tenant = await insert_tenant(conn)
            return [
                await insert_shop(conn, tenant, state="not_connected", merchant_uid=None),
                await insert_shop(conn, tenant, state="awaiting_code", merchant_uid=None),
                await insert_shop(
                    conn,
                    tenant,
                    state="reauth_required",
                    error="wrong_credentials",
                    merchant_uid=None,
                ),
            ]

        _run(url, seed)
        command.upgrade(config, "0010")

        assert _run(url, _shop_ids) == set()


def test_connected_shops_stay_untouched_in_mixed_install() -> None:
    """Т11, Т12: подключённые (в том числе reauth_required с uid) и их строки остаются."""
    with _database_at_0009() as (url, config):

        async def seed(conn: AsyncConnection) -> dict[str, Any]:
            tenant_a = await insert_tenant(conn)
            tenant_b = await insert_tenant(conn)
            kept: list[uuid.UUID] = []
            dropped: list[uuid.UUID] = []
            for tenant, states in (
                (tenant_a, ["connected", None, None]),
                (tenant_b, ["connected", "reauth_required", None]),
            ):
                for state in states:
                    if state is None:
                        shop = await insert_shop(
                            conn, tenant, state="not_connected", merchant_uid=None
                        )
                        dropped.append(shop)
                        continue
                    shop = await insert_shop(conn, tenant, state=state, error=None)
                    kept.append(shop)
                    product = await insert_product(conn, tenant, shop)
                    await insert_change(conn, tenant, shop, product)
                    await insert_write(conn, tenant, shop, product, status="unknown")
            return {"a": tenant_a, "b": tenant_b, "kept": kept, "dropped": dropped}

        seeded = _run(url, seed)

        async def snapshot(conn: AsyncConnection) -> dict[str, list[dict[str, Any]]]:
            tables = ("shop", "product", "price_change", "price_write")
            out = {}
            for table in tables:
                key = "id" if table == "shop" else "shop_id"
                result = await conn.execute(
                    text(f"SELECT * FROM {table} WHERE {key} = ANY(:s) ORDER BY 1, 2"),
                    {"s": seeded["kept"]},
                )
                out[table] = [dict(row) for row in result.mappings()]
            return out

        before = _run(url, snapshot)
        # insert_write добавляет свой price_change, поэтому их шесть
        assert [len(rows) for rows in before.values()] == [3, 3, 6, 3]
        command.upgrade(config, "0010")

        assert _run(url, snapshot) == before
        assert _run(url, _shop_ids) == set(seeded["kept"])
        counts = _run(url, _tenant_counts)
        assert counts == {seeded["a"]: 1, seeded["b"]: 2}


def test_owner_event_of_removed_shop_stays() -> None:
    """Т13: журнал действий не удаляется вместе с магазином."""
    with _database_at_0009() as (url, config):

        async def seed(conn: AsyncConnection) -> uuid.UUID:
            tenant = await insert_tenant(conn)
            shop = await insert_shop(conn, tenant, state="not_connected", merchant_uid=None)
            user = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO app_user (id, tenant_id, email, password_hash) "
                    "VALUES (:u, :t, 'm@example.kz', 'h')"
                ),
                {"u": user, "t": tenant},
            )
            await conn.execute(
                text(
                    "INSERT INTO owner_event (tenant_id, user_id, kind, ip, shop_id, "
                    "old_value, new_value) "
                    "VALUES (:t, :u, 'bot_mode_changed', '1.1.1.1', :s, 'live', 'dry_run')"
                ),
                {"t": tenant, "u": user, "s": shop},
            )
            return shop

        shop = _run(url, seed)
        command.upgrade(config, "0010")

        async def events(conn: AsyncConnection) -> list[Any]:
            return [row[0] for row in await conn.execute(text("SELECT shop_id FROM owner_event"))]

        assert _run(url, _shop_ids) == set()
        assert _run(url, events) == [shop]


def test_shop_without_uid_but_with_product_stays_and_downgrade_works() -> None:
    """Т14: предохранитель «нет товаров»; downgrade и повторный upgrade проходят."""
    with _database_at_0009() as (url, config):

        async def seed(conn: AsyncConnection) -> uuid.UUID:
            tenant = await insert_tenant(conn)
            shop = await insert_shop(conn, tenant, state="not_connected", merchant_uid=None)
            await insert_product(conn, tenant, shop)
            return shop

        shop = _run(url, seed)
        command.upgrade(config, "0010")
        assert _run(url, _shop_ids) == {shop}

        command.downgrade(config, "0009")
        assert _run(url, _shop_ids) == {shop}
        command.upgrade(config, "0010")
        assert _run(url, _shop_ids) == {shop}


def test_connected_shop_without_products_stays_and_upgrade_is_idempotent() -> None:
    """Подключённый магазин без товаров не удаляется; повторная выдача `0010` ничего не меняет."""
    with _database_at_0009() as (url, config):

        async def seed(conn: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
            tenant = await insert_tenant(conn)
            connected = await insert_shop(conn, tenant, state="connected")
            lost = await insert_shop(conn, tenant, state="reauth_required", error="x")
            return connected, lost

        connected, lost = _run(url, seed)
        command.upgrade(config, "0010")
        assert _run(url, _shop_ids) == {connected, lost}

        command.downgrade(config, "0009")
        command.upgrade(config, "0010")
        assert _run(url, _shop_ids) == {connected, lost}
