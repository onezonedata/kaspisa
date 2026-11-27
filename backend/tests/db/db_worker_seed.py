"""Начальные данные тестов обработчика: сырые INSERT, чтобы задавать любое состояние строк."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_AUTO_UID = "<auto>"  # метка «сгенерировать merchant_uid»; None — без merchant_uid
NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
_FRESH: Any = object()  # метка «каталог загружен только что»; None — загрузки не было


def ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


async def insert_tenant(conn: AsyncConnection) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    await conn.execute(text("INSERT INTO tenant (id, name) VALUES (:id, 't')"), {"id": tenant_id})
    return tenant_id


async def insert_shop(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    *,
    state: str = "connected",
    error: str | None = None,
    bot_mode: str = "live",
    halt_reason: str | None = None,
    city_id: str | None = "710000000",
    merchant_uid: str | None = _AUTO_UID,
    claimed_by: str | None = None,
    claimed_until: datetime | None = None,
    catalog_synced_at: datetime | None = _FRESH,
    catalog_retry_at: datetime | None = None,
) -> uuid.UUID:
    shop_id = uuid.uuid4()
    uid = f"m-{secrets.token_hex(6)}" if merchant_uid == _AUTO_UID else merchant_uid
    await conn.execute(
        text(
            "INSERT INTO shop (id, tenant_id, merchant_uid, login_enc, password_enc, session_enc, "
            "connection_state, connection_error, bot_mode, city_id) "
            "VALUES (:id, :t, :uid, :b, :b, :b, :state, :error, :bot, :city)"
        ),
        {
            "id": shop_id,
            "t": tenant_id,
            "uid": uid,
            "b": b"x",
            "state": state,
            "error": error,
            "bot": bot_mode,
            "city": city_id,
        },
    )
    synced = NOW if catalog_synced_at is _FRESH else catalog_synced_at
    await conn.execute(
        text("UPDATE shop SET catalog_synced_at = :s, catalog_retry_at = :r WHERE id = :id"),
        {"s": synced, "r": catalog_retry_at, "id": shop_id},
    )
    if halt_reason is not None:
        await conn.execute(
            text("UPDATE shop SET halt_reason = :r, halted_at = :at WHERE id = :id"),
            {"r": halt_reason, "at": NOW, "id": shop_id},
        )
    if claimed_by is not None:
        await conn.execute(
            text("UPDATE shop SET claimed_by = :w, claimed_until = :u WHERE id = :id"),
            {"w": claimed_by, "u": claimed_until, "id": shop_id},
        )
    return shop_id


async def insert_product(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    *,
    tracked: bool = True,
    min_price: int | None = 100,
    max_price: int | None = 200,
    last_checked_at: datetime | None = None,
    retry_at: datetime | None = None,
) -> int:
    sku = f"sku-{secrets.token_hex(4)}"
    result = await conn.execute(
        text(
            "INSERT INTO product (tenant_id, shop_id, sku, master_sku, title, step, tracked, "
            "min_price, max_price, last_checked_at, retry_at) "
            "VALUES (:t, :s, :sku, 'm', 'Товар', 1, :tracked, :mn, :mx, :lc, :retry) RETURNING id"
        ),
        {
            "t": tenant_id,
            "s": shop_id,
            "sku": sku,
            "tracked": tracked,
            "mn": min_price,
            "mx": max_price,
            "lc": last_checked_at,
            "retry": retry_at,
        },
    )
    return int(result.scalar_one())


async def insert_change(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    product_id: int,
    *,
    result: str = "unconfirmed",
    created_at: datetime = NOW,
) -> int:
    row = await conn.execute(
        text(
            "INSERT INTO price_change (tenant_id, shop_id, product_id, created_at, old_price, "
            "new_price, reason, result) "
            "VALUES (:t, :s, :p, :c, 100, 90, 'undercut_first', :r) RETURNING id"
        ),
        {"t": tenant_id, "s": shop_id, "p": product_id, "c": created_at, "r": result},
    )
    return int(row.scalar_one())


async def insert_write(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    product_id: int,
    *,
    status: str = "receipt",
    sent_at: datetime = NOW,
    next_verify_at: datetime | None = NOW,
) -> int:
    change_id = await insert_change(conn, tenant_id, shop_id, product_id, created_at=sent_at)
    await conn.execute(
        text(
            "INSERT INTO price_write (product_id, tenant_id, shop_id, sku, snapshot, "
            "expected_prices, sent_at, status, next_verify_at, price_change_id) "
            "VALUES (:p, :t, :s, 'sku', CAST(:snap AS jsonb), CAST(:exp AS jsonb), :sent, "
            ":status, :nv, :c)"
        ),
        {
            "p": product_id,
            "t": tenant_id,
            "s": shop_id,
            "snap": json.dumps({}),
            "exp": json.dumps({"710000000": 90}),
            "sent": sent_at,
            "status": status,
            "nv": next_verify_at,
            "c": change_id,
        },
    )
    return change_id


async def scalar(conn: AsyncConnection, sql: str, **params: Any) -> Any:
    return (await conn.execute(text(sql), params)).scalar_one()
