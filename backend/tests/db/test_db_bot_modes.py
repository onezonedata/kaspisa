"""ADR 0024, Т1-Т2: миграция 0008 (режим бота, якорь минимума, журнал владельца) и ограничения."""

from __future__ import annotations

import asyncio
import uuid
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


async def _query(url: str, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return [tuple(row) for row in result.all()] if result.returns_rows else []
    finally:
        await engine.dispose()


def _run(url: str, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    return asyncio.run(_query(url, sql, **params))


def _seed_0007(url: str) -> list[uuid.UUID]:
    tenant = uuid.uuid4()
    _run(url, "INSERT INTO tenant (id, name) VALUES (:i, 't')", i=tenant)
    shops = [uuid.uuid4() for _ in range(3)]
    for shop, bot in zip(shops, (True, False, True), strict=True):
        _run(
            url,
            "INSERT INTO shop (id, tenant_id, login_enc, password_enc, bot_enabled) "
            "VALUES (:i, :t, :b, :b, :bot)",
            i=shop,
            t=tenant,
            b=b"x",
            bot=bot,
        )
    for shop, minimum in ((shops[0], 700), (shops[1], None)):
        _run(
            url,
            "INSERT INTO product (tenant_id, shop_id, sku, master_sku, title, step, min_price) "
            "VALUES (:t, :s, :sku, 'm', 'Товар', 1, :mn)",
            t=tenant,
            s=shop,
            sku=f"sku-{shop}",
            mn=minimum,
        )
    return shops


_COLUMN = (
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
)
_TABLE = "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'owner_event'"


def _has_column(url: str, table: str, column: str) -> bool:
    return bool(_run(url, _COLUMN, t=table, c=column))


def test_migration_0008_converts_modes_anchor_and_rolls_back_safely() -> None:
    """Т1: true -> dry_run, false -> off; якорь = минимум; откат даёт true только для live."""
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0007")
        shops = _seed_0007(url)
        assert not _run(url, _TABLE)

        command.upgrade(config, "0009")  # не head: 0010 удаляет засеянные магазины без uid
        modes = dict(_run(url, "SELECT id, bot_mode FROM shop"))
        assert [modes[s] for s in shops] == ["dry_run", "off", "dry_run"]
        anchors = _run(
            url,
            "SELECT s.id, p.min_price_anchor FROM product p JOIN shop s ON s.id = p.shop_id",
        )
        assert dict(anchors) == {shops[0]: 700, shops[1]: None}
        assert not _has_column(url, "shop", "bot_enabled")
        assert _has_column(url, "product", "min_price_anchor")
        assert _run(url, _TABLE)

        for shop, mode in zip(shops, ("live", "dry_run", "off"), strict=True):
            _run(url, "UPDATE shop SET bot_mode = :m WHERE id = :i", m=mode, i=shop)
        command.downgrade(config, "0007")
        enabled = dict(_run(url, "SELECT id, bot_enabled FROM shop"))
        assert [enabled[s] for s in shops] == [True, False, False]
        assert not _has_column(url, "shop", "bot_mode")
        assert not _has_column(url, "product", "min_price_anchor")
        assert not _run(url, _TABLE)

        command.upgrade(config, "head")
        assert _run(url, _TABLE)
    finally:
        drop_test_database_sync(name)


async def _violates(conn: AsyncConnection, sql: str, fragment: str, **params: Any) -> None:
    with pytest.raises(IntegrityError) as caught:
        async with conn.begin_nested():
            await conn.execute(text(sql), params)
    assert fragment in str(caught.value.orig)


async def test_new_shop_is_off_and_modes_are_checked(connection: AsyncConnection) -> None:
    """Т2: умолчание `off`; недопустимый режим и нулевой якорь отвергаются CHECK."""
    tenant = await insert_tenant(connection)
    shop = await connection.execute(
        text(
            "INSERT INTO shop (id, tenant_id, login_enc, password_enc) "
            "VALUES (gen_random_uuid(), :t, :b, :b) RETURNING id, bot_mode"
        ),
        {"t": tenant, "b": b"x"},
    )
    shop_id, mode = shop.one()
    assert mode == "off"
    await _violates(
        connection,
        "UPDATE shop SET bot_mode = 'on' WHERE id = :i",
        "ck_shop_bot_mode",
        i=shop_id,
    )
    live = await insert_shop(connection, tenant, bot_mode="live")
    product = await insert_product(connection, tenant, live)
    await _violates(
        connection,
        "UPDATE product SET min_price_anchor = 0 WHERE id = :i",
        "ck_product_min_price_anchor_positive",
        i=product,
    )
    await connection.execute(
        text("UPDATE product SET min_price_anchor = 1 WHERE id = :i"), {"i": product}
    )
