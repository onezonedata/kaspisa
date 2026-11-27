"""Т1: ревизия 0002 — столбцы обработчика и таблица price_write."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from db_worker_seed import (
    NOW,
    insert_change,
    insert_product,
    insert_shop,
    insert_tenant,
    insert_write,
    scalar,
)
from sqlalchemy import NullPool, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command

NEW_SHOP_COLUMNS = {
    "claimed_by",
    "claimed_until",
    "halt_reason",
    "halted_at",
    "login_error_streak",
    "write_error_streak",
    "unproven_login_version",
}
NEW_PRODUCT_COLUMNS = {
    "tracked",
    "refusal_reason",
    "refusal_since",
    "refusal_fingerprint",
    "retry_at",
}


async def _column_values(url: str, sql: str) -> Sequence[str]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            return [row[0] for row in await conn.execute(text(sql))]
    finally:
        await engine.dispose()


def _columns(url: str, table: str) -> set[str]:
    sql = (
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = 'public' AND table_name = '{table}'"
    )
    return set(asyncio.run(_column_values(url, sql)))


def _tables(url: str) -> set[str]:
    sql = "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    return set(asyncio.run(_column_values(url, sql)))


def test_upgrade_adds_and_downgrade_removes_worker_schema() -> None:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0001")
        assert "price_write" not in _tables(url)
        assert not NEW_SHOP_COLUMNS & _columns(url, "shop")

        command.upgrade(config, "head")
        assert "price_write" in _tables(url)
        assert _columns(url, "shop") >= NEW_SHOP_COLUMNS
        assert _columns(url, "product") >= NEW_PRODUCT_COLUMNS

        command.downgrade(config, "0001")
        assert "price_write" not in _tables(url)
        assert not NEW_SHOP_COLUMNS & _columns(url, "shop")
        assert not NEW_PRODUCT_COLUMNS & _columns(url, "product")

        command.upgrade(config, "head")
        assert "price_write" in _tables(url)
    finally:
        drop_test_database_sync(name)


async def test_price_write_error_counted_column_defaults_to_false(
    connection: AsyncConnection,
) -> None:
    """Н9: признак «ошибка учтена при отправке» есть и по умолчанию ложен."""
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product, status="unknown")
    assert (
        await scalar(
            connection, "SELECT error_counted FROM price_write WHERE product_id = :p", p=product
        )
        is False
    )
    nullable = await scalar(
        connection,
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'price_write' AND column_name = 'error_counted'",
    )
    assert nullable == "NO"


async def test_defaults_of_new_columns(connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    row = (
        await connection.execute(
            text(
                "SELECT login_error_streak, write_error_streak, claimed_by, claimed_until, "
                "halt_reason, halted_at, unproven_login_version FROM shop WHERE id = :id"
            ),
            {"id": shop},
        )
    ).one()
    assert tuple(row) == (0, 0, None, None, None, None, None)
    row = (
        await connection.execute(
            text(
                "SELECT refusal_reason, refusal_since, refusal_fingerprint, retry_at "
                "FROM product WHERE id = :id"
            ),
            {"id": product},
        )
    ).one()
    assert tuple(row) == (None, None, None, None)
    # по умолчанию товар не отслеживается
    result = await connection.execute(
        text(
            "INSERT INTO product (tenant_id, shop_id, sku, master_sku, title, step) "
            "VALUES (:t, :s, 'x', 'm', 'T', 1) RETURNING tracked"
        ),
        {"t": tenant, "s": shop},
    )
    assert result.scalar_one() is False


@pytest.mark.parametrize(
    "assignment",
    [
        "halt_reason = 'x'",
        "halted_at = now()",
        "login_error_streak = -1",
        "write_error_streak = -1",
    ],
)
async def test_shop_checks(connection: AsyncConnection, assignment: str) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(f"UPDATE shop SET {assignment} WHERE id = :id"), {"id": shop}
            )


async def test_price_write_constraints(connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product)

    # CHECK статуса
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text("UPDATE price_write SET status = 'bogus' WHERE product_id = :p"),
                {"p": product},
            )
    for status in ("sending", "receipt", "unknown", "confirmed", "expired"):
        await connection.execute(
            text("UPDATE price_write SET status = :s WHERE product_id = :p"),
            {"s": status, "p": product},
        )

    # составной внешний ключ: строка чужого арендатора на свой товар невозможна
    product2 = await insert_product(connection, tenant, shop)
    change = await insert_change(connection, tenant, shop, product2)
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(
                text(
                    "INSERT INTO price_write (product_id, tenant_id, shop_id, sku, snapshot, "
                    "expected_prices, sent_at, status, price_change_id) VALUES "
                    "(:p, :t, :s, 'k', '{}'::jsonb, '{}'::jsonb, :at, 'sending', :c)"
                ),
                {"p": product2, "t": other, "s": shop, "at": NOW, "c": change},
            )

    # tenant_id неизменяем
    with pytest.raises(DBAPIError):
        async with connection.begin_nested():
            await connection.execute(
                text("UPDATE price_write SET tenant_id = :t WHERE product_id = :p"),
                {"t": other, "p": product},
            )


async def test_price_write_is_removed_with_product(connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product)
    assert await scalar(connection, "SELECT count(*) FROM price_write") == 1
    await connection.execute(text("DELETE FROM product WHERE id = :p"), {"p": product})
    assert await scalar(connection, "SELECT count(*) FROM price_write") == 0
