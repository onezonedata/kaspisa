"""ADR 0017, Т9: имя конкурента в журнале берётся из прочитанных предложений карточки."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from worker_testing import (
    Rig,
    execute,
    fetch,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.kaspi.models import Offer, OffersPage


def _page(own_merchant: str, *others: tuple[str, str, int], own: int | None = 1500) -> OffersPage:
    offers = [
        Offer.model_validate({"merchantId": m, "merchantName": n, "price": price})
        for m, n, price in others
    ]
    if own is not None:
        offers.insert(
            0,
            Offer.model_validate(
                {"merchantId": own_merchant, "merchantName": "Свой", "price": own}
            ),
        )
    return OffersPage(offers=offers, total=len(offers))


async def _bounds(rig: Rig, p: Prepared, *, auto_raise: bool = False) -> None:
    await execute(
        rig,
        "UPDATE product SET min_price=1000, max_price=2000, step=1, auto_raise=:r WHERE id=:i",
        i=p.product_id,
        r=auto_raise,
    )


async def _journal(rig: Rig, p: Prepared) -> list[tuple[Any, ...]]:
    return await fetch(
        rig,
        "SELECT new_price, result, competitor_merchant_id, competitor_name, competitor_price "
        "FROM price_change ORDER BY id",
    )


async def test_dry_run_journal_has_competitor_name(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    await _bounds(rig, p)
    p.client.price = 1500
    p.client.offers_page = _page(p.shop.merchant_uid, ("m-a", "Магазин А", 1200))
    assert await run_one_slice(p.ctx, p.database)
    rows = await _journal(rig, p)
    assert len(rows) == 1
    assert rows[0][1:] == ("dry_run", "m-a", "Магазин А", 1200)


async def test_no_competitor_means_no_name(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    await _bounds(rig, p, auto_raise=True)
    await execute(rig, "UPDATE shop SET auto_raise_enabled=true WHERE id=:i", i=p.shop.shop_id)
    p.client.price = 1500
    p.client.offers_page = _page(p.shop.merchant_uid)
    assert await run_one_slice(p.ctx, p.database)
    rows = await _journal(rig, p)
    assert len(rows) == 1
    assert rows[0][0] == 2000
    assert rows[0][2:] == (None, None, None)


async def test_real_write_reserve_row_has_competitor_name(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    await _bounds(rig, p)
    p.client.price = 1500
    p.client.offers_page = _page(p.shop.merchant_uid, ("m-a", "Магазин А", 1200))
    assert await run_one_slice(p.ctx, p.database)
    assert len(p.client.sent) == 1
    rows = await _journal(rig, p)
    assert len(rows) == 1
    assert rows[0][1:] == ("unconfirmed", "m-a", "Магазин А", 1200)
