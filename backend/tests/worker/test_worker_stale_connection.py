"""Версия подключения сменилась после открытия доступа: старая сессия не используется."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import timedelta

from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    execute,
    fetch,
    make_worker_ctx,
    open_slice_ctx,
    redis_client,
    redis_prefix,
    rig,
    seed_shop,
    worker_cipher,
    worker_database_url,
)

from kaspisa.kaspi import KaspiSession, SessionExpiredError
from kaspisa.kaspi.models import ListedProduct
from kaspisa.worker.catalog_refresh import refresh_catalog_if_due
from kaspisa.worker.slice import _work


async def _claim(rig: Rig, shop_id: object) -> None:
    await execute(
        rig,
        "UPDATE shop SET claimed_by='w1', claimed_until=:u WHERE id=:i",
        u=NOW + timedelta(seconds=180),
        i=shop_id,
    )


async def _bump_version(rig: Rig, shop_id: object) -> None:
    await execute(
        rig, "UPDATE shop SET connection_version = connection_version + 1 WHERE id=:i", i=shop_id
    )


async def test_catalog_step_skipped_when_version_changed(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=0, catalog_synced_at=None)
    client = FakeKaspiClient()
    client.merchant_uid = shop.merchant_uid
    client.session_changed = True
    ctx = make_worker_ctx(database, client, redis_client, redis_prefix, FakeClock())
    slice_ctx = await open_slice_ctx(ctx, shop)
    await _claim(rig, shop.shop_id)
    before = await fetch(rig, "SELECT session_enc FROM shop WHERE id=:i", i=shop.shop_id)

    await _bump_version(rig, shop.shop_id)
    await refresh_catalog_if_due(slice_ctx, None)

    assert client.methods() == []
    after = await fetch(
        rig,
        "SELECT session_enc, catalog_synced_at, catalog_retry_at FROM shop WHERE id=:i",
        i=shop.shop_id,
    )
    assert after[0][0] == before[0][0]
    assert after[0][1:] == (None, None)


async def test_price_checks_skipped_when_version_changed(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=1)
    client = FakeKaspiClient()
    client.merchant_uid = shop.merchant_uid
    ctx = make_worker_ctx(database, client, redis_client, redis_prefix, FakeClock())
    slice_ctx = await open_slice_ctx(ctx, shop)
    await _claim(rig, shop.shop_id)

    await _bump_version(rig, shop.shop_id)
    await _work(slice_ctx, NOW, None)

    assert client.methods() == []


async def test_reconnect_during_list_products_keeps_new_session(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=0, catalog_synced_at=None)
    client = FakeKaspiClient()
    client.merchant_uid = shop.merchant_uid
    new_blob = b"new-session-from-reconnect"

    async def reconnect_then_expire(
        session: KaspiSession, merchant_uid: str
    ) -> list[ListedProduct]:
        # продавец переподключил магазин, пока шёл запрос; ответ Kaspi меняет cookie и даёт 401
        await execute(
            rig,
            "UPDATE shop SET connection_version = connection_version + 1, session_enc=:b "
            "WHERE id=:i",
            b=new_blob,
            i=shop.shop_id,
        )
        session.changed = True
        raise SessionExpiredError("list_products")

    client.list_products = reconnect_then_expire  # type: ignore[method-assign]
    ctx = make_worker_ctx(database, client, redis_client, redis_prefix, FakeClock())
    slice_ctx = await open_slice_ctx(ctx, shop)
    await _claim(rig, shop.shop_id)

    await refresh_catalog_if_due(slice_ctx, None)

    rows = await fetch(
        rig, "SELECT session_enc, connection_state FROM shop WHERE id=:i", i=shop.shop_id
    )
    assert bytes(rows[0][0]) == new_blob
    assert rows[0][1] != "reauth_required"
