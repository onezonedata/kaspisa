"""Т27, Т22 (прямой SQL): ограничения схемы и неизменяемость tenant_id."""

# SQL собирается из констант теста (имена таблиц и столбцов), не из внешних данных.

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

Params = Mapping[str, Any]


def _constraint_name(exc: DBAPIError) -> str | None:
    cause = exc.orig.__cause__ if exc.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


async def _insert_tenant(conn: AsyncConnection) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await conn.execute(text("INSERT INTO tenant (id, name) VALUES (:id, 'T')"), {"id": tenant_id})
    return tenant_id


async def _insert_user(
    conn: AsyncConnection, tenant_id: uuid.UUID, email: str = "a@x.kz"
) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO app_user (id, tenant_id, email, password_hash) VALUES (:i, :t, :e, 'h')"),
        {"i": user_id, "t": tenant_id, "e": email},
    )
    return user_id


async def _insert_shop(conn: AsyncConnection, tenant_id: uuid.UUID, **extra: Any) -> uuid.UUID:
    shop_id = uuid.uuid4()
    columns = {"id": shop_id, "tenant_id": tenant_id, "login_enc": b"l", "password_enc": b"p"}
    columns.update(extra)
    names = ", ".join(columns)
    marks = ", ".join(f":{k}" for k in columns)
    await conn.execute(text(f"INSERT INTO shop ({names}) VALUES ({marks})"), columns)
    return shop_id


async def _insert_product(
    conn: AsyncConnection, tenant_id: uuid.UUID, shop_id: uuid.UUID, **extra: Any
) -> int:
    columns: dict[str, Any] = {
        "tenant_id": tenant_id,
        "shop_id": shop_id,
        "sku": uuid.uuid4().hex,
        "master_sku": "m",
        "title": "t",
        "step": 1,
    }
    columns.update(extra)
    names = ", ".join(columns)
    marks = ", ".join(f":{k}" for k in columns)
    result = await conn.execute(
        text(f"INSERT INTO product ({names}) VALUES ({marks}) RETURNING id"),
        columns,
    )
    return int(result.scalar_one())


async def _insert_change(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    product_id: int,
    **extra: Any,
) -> int:
    columns: dict[str, Any] = {
        "tenant_id": tenant_id,
        "shop_id": shop_id,
        "product_id": product_id,
        "old_price": 100,
        "new_price": 99,
        "reason": "undercut_first",
        "result": "applied",
    }
    columns.update(extra)
    names = ", ".join(columns)
    marks = ", ".join(f":{k}" for k in columns)
    result = await conn.execute(
        text(f"INSERT INTO price_change ({names}) VALUES ({marks}) RETURNING id"),
        columns,
    )
    return int(result.scalar_one())


async def _rejected(conn: AsyncConnection, sql: str, params: Params, name: str) -> None:
    """Запрос отклонён ограничением с данным именем; транзакция теста остаётся рабочей."""
    with pytest.raises(IntegrityError) as info:
        async with conn.begin_nested():
            await conn.execute(text(sql), params)
    assert _constraint_name(info.value) == name


async def test_product_price_constraints(connection: AsyncConnection) -> None:
    tenant = await _insert_tenant(connection)
    shop = await _insert_shop(connection, tenant)
    product = await _insert_product(connection, tenant, shop)

    bad = {
        "min_price = 0": "ck_product_min_price_positive",
        "max_price = 0": "ck_product_max_price_positive",
        "min_price = 500, max_price = 100": "ck_product_min_not_above_max",
        "step = 0": "ck_product_step_positive",
        "current_price = 0": "ck_product_current_price_positive",
        "position = 0": "ck_product_position_positive",
        "position_over = 0": "ck_product_position_over_positive",
        "position = 3, position_over = 20": "ck_product_position_or_over",
        "competitors_count = -1": "ck_product_competitors_count_not_negative",
        "first_place_price = 0": "ck_product_first_place_price_positive",
    }
    for assignment, name in bad.items():
        await _rejected(
            connection,
            f"UPDATE product SET {assignment} WHERE id = :id",
            {"id": product},
            name,
        )

    # Товар без границ и с min = max допустим.
    await connection.execute(
        text("UPDATE product SET min_price = 100, max_price = 100 WHERE id = :id"), {"id": product}
    )
    await _insert_product(connection, tenant, shop, min_price=None, max_price=None)


async def test_shop_setting_constraints(connection: AsyncConnection) -> None:
    tenant = await _insert_tenant(connection)
    shop = await _insert_shop(connection, tenant)
    bad = {
        "default_step = 0": "ck_shop_default_step_positive",
        "strategy = 'unknown'": "ck_shop_strategy",
        "connection_state = 'unknown'": "ck_shop_connection_state",
        # connected без merchant_uid или без session_enc
        "connection_state = 'connected'": "ck_shop_connected_requires_session",
        "connection_state = 'connected', merchant_uid = 'm1'": "ck_shop_connected_requires_session",
        "connection_state = 'connected', session_enc = 's'": "ck_shop_connected_requires_session",
    }
    for assignment, name in bad.items():
        await _rejected(
            connection,
            f"UPDATE shop SET {assignment} WHERE id = :id",
            {"id": shop},
            name,
        )
    await connection.execute(
        text(
            "UPDATE shop SET connection_state = 'connected', merchant_uid = 'm1', "
            "session_enc = 's' WHERE id = :id"
        ),
        {"id": shop},
    )


async def test_shop_defaults(connection: AsyncConnection) -> None:
    tenant = await _insert_tenant(connection)
    shop = await _insert_shop(connection, tenant)
    row = (
        (await connection.execute(text("SELECT * FROM shop WHERE id = :id"), {"id": shop}))
        .mappings()
        .one()
    )
    assert row["connection_state"] == "not_connected"
    assert row["connection_version"] == 0
    assert row["bot_mode"] == "off"
    assert "bot_enabled" not in row
    assert row["auto_raise_enabled"] is True
    assert row["strategy"] == "be_first"
    assert "below_min" not in row
    assert row["default_step"] == 1
    assert row["excluded_merchant_ids"] == []
    assert row["merchant_uid"] is None


async def test_app_user_constraints(connection: AsyncConnection) -> None:
    tenant = await _insert_tenant(connection)
    await _insert_user(connection, tenant, "a@x.kz")
    await _rejected(
        connection,
        "INSERT INTO app_user (id, tenant_id, email, password_hash) VALUES (:i, :t, 'B@x.kz', 'h')",
        {"i": uuid.uuid4(), "t": tenant},
        "ck_app_user_email_lowercase",
    )
    await _rejected(
        connection,
        "INSERT INTO app_user (id, tenant_id, email, password_hash) VALUES (:i, :t, 'a@x.kz', 'h')",
        {"i": uuid.uuid4(), "t": tenant},
        "uq_app_user_email",
    )


async def test_price_change_constraints(connection: AsyncConnection) -> None:
    tenant = await _insert_tenant(connection)
    shop = await _insert_shop(connection, tenant)
    product = await _insert_product(connection, tenant, shop)
    change = await _insert_change(connection, tenant, shop, product)
    bad = {
        "new_price = old_price": "ck_price_change_price_changed",
        "old_price = 0": "ck_price_change_old_price_positive",
        "new_price = 0": "ck_price_change_new_price_positive",
        "skipped_competitors = -1": "ck_price_change_skipped_competitors_not_negative",
        "result = 'unknown'": "ck_price_change_result",
    }
    for assignment, name in bad.items():
        await _rejected(
            connection,
            f"UPDATE price_change SET {assignment} WHERE id = :id",
            {"id": change},
            name,
        )
    # reason без CHECK: журнал переживает изменение списка причин.
    await _insert_change(connection, tenant, shop, product, reason="reason_from_the_future")


async def test_unique_merchant_uid_and_sku(connection: AsyncConnection) -> None:
    t1 = await _insert_tenant(connection)
    t2 = await _insert_tenant(connection)
    await _insert_shop(connection, t1, merchant_uid="m-1")
    # Несколько магазинов без merchant_uid допустимы.
    await _insert_shop(connection, t1)
    await _insert_shop(connection, t2)
    with pytest.raises(IntegrityError) as info:
        async with connection.begin_nested():
            await _insert_shop(connection, t2, merchant_uid="m-1")
    assert _constraint_name(info.value) == "uq_shop_merchant_uid"

    shop = await _insert_shop(connection, t1)
    await _insert_product(connection, t1, shop, sku="dup")
    with pytest.raises(IntegrityError) as info:
        async with connection.begin_nested():
            await _insert_product(connection, t1, shop, sku="dup")
    assert _constraint_name(info.value) == "uq_product_shop_id_sku"


async def test_composite_foreign_keys_block_cross_tenant_links(connection: AsyncConnection) -> None:
    a = await _insert_tenant(connection)
    b = await _insert_tenant(connection)
    shop_a = await _insert_shop(connection, a)
    product_a = await _insert_product(connection, a, shop_a)

    with pytest.raises(IntegrityError) as info:
        async with connection.begin_nested():
            await _insert_product(connection, b, shop_a)
    assert _constraint_name(info.value) == "fk_product_tenant_id_shop_id_shop"

    with pytest.raises(IntegrityError) as info:
        async with connection.begin_nested():
            await _insert_change(connection, b, shop_a, product_a)
    assert _constraint_name(info.value) in {
        "fk_price_change_tenant_id_shop_id_shop",
        "fk_price_change_tenant_id_product_id_product",
    }


async def test_tenant_id_is_immutable_on_all_tables(connection: AsyncConnection) -> None:
    a = await _insert_tenant(connection)
    b = await _insert_tenant(connection)
    user = await _insert_user(connection, a)
    lonely_shop = await _insert_shop(connection, a)  # без товаров и журнала
    shop = await _insert_shop(connection, a)
    product = await _insert_product(connection, a, shop)
    change = await _insert_change(connection, a, shop, product)

    targets = [
        ("app_user", user),
        ("shop", lonely_shop),
        ("product", product),
        ("price_change", change),
    ]
    for table, row_id in targets:
        await _rejected(
            connection,
            f"UPDATE {table} SET tenant_id = :b WHERE id = :id",
            {"b": b, "id": row_id},
            "tenant_id_immutable",
        )
        # UPDATE с тем же значением tenant_id проходит.
        await connection.execute(
            text(f"UPDATE {table} SET tenant_id = :a WHERE id = :id"),
            {"a": a, "id": row_id},
        )
        owner = (
            await connection.execute(
                text(f"SELECT tenant_id FROM {table} WHERE id = :id"),
                {"id": row_id},
            )
        ).scalar_one()
        assert owner == a


async def test_tenant_delete_cascades(connection: AsyncConnection) -> None:
    a = await _insert_tenant(connection)
    shop = await _insert_shop(connection, a)
    product = await _insert_product(connection, a, shop)
    await _insert_change(connection, a, shop, product)
    await _insert_user(connection, a)
    await connection.execute(text("DELETE FROM tenant WHERE id = :id"), {"id": a})
    for table in ("app_user", "shop", "product", "price_change"):
        count = (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        assert count == 0
