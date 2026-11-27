"""Цикл товара на неполном списке продавцов карточки (ADR 0016)."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from redis.asyncio import Redis
from worker_testing import (
    FakeKaspiClient,
    Rig,
    execute,
    fetch,
    journal,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.kaspi.models import Offer, OffersPage


def _offer(merchant: str, price: int) -> Offer:
    return Offer.model_validate({"merchantId": merchant, "merchantName": merchant, "price": price})


async def _prepare_raising(rig: Rig, redis: Redis, prefix: str) -> Prepared:
    """Режим без записи; повышение включено у товара и у магазина."""
    database = rig.new_database()
    shop = await seed_shop(rig, database)
    await execute(rig, "UPDATE product SET auto_raise = true")
    await execute(rig, "UPDATE shop SET auto_raise_enabled = true")
    return await prepare(
        rig, redis, prefix, dry_run=True, database=database, shop=shop, client=FakeKaspiClient()
    )


OBSERVED = "SELECT current_price, position, position_over, competitors_count, first_place_price "
OBSERVED += "FROM product"


async def test_own_absent_on_two_pages_is_over_and_no_decision(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Своего магазина нет среди 20 прочитанных из 57: позиция «20+», число конкурентов 57,
    цена не меняется (OWN_OFFER_MISSING)."""
    client = FakeKaspiClient()
    client.offers_page = OffersPage(offers=[_offer(f"m{i}", 50 + i) for i in range(20)], total=57)
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True, client=client)
    await run_one_slice(p.ctx, p.database)
    assert await fetch(rig, OBSERVED) == [(100, None, 20, 57, 50)]
    assert await journal(rig, p.shop.shop_id) == [
        (100, 100, "own_offer_missing", "no_change", None)
    ]


async def test_no_competitors_read_but_more_in_card_does_not_raise(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Среди прочитанных только свой магазин, а в карточке ещё 29 продавцов:
    до максимальной цена не поднимается (COMPETITORS_UNSEEN)."""
    p = await _prepare_raising(rig, redis_client, redis_prefix)
    client = p.client
    client.offers_page = OffersPage(offers=[_offer(p.shop.merchant_uid, 100)], total=30)
    await run_one_slice(p.ctx, p.database)
    assert await fetch(rig, OBSERVED) == [(100, 1, None, 29, 100)]
    assert await journal(rig, p.shop.shop_id) == [
        (100, 100, "competitors_unseen", "no_change", None)
    ]


async def test_alone_in_whole_card_still_raises(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Контроль: карточка прочитана целиком и конкурентов нет - правило 1 действует."""
    p = await _prepare_raising(rig, redis_client, redis_prefix)
    client = p.client
    client.offers_page = OffersPage(offers=[_offer(p.shop.merchant_uid, 100)], total=1)
    await run_one_slice(p.ctx, p.database)
    rows = await journal(rig, p.shop.shop_id)
    assert [(r[2], r[3]) for r in rows] == [("no_competitors_raised_to_max", "dry_run")]


async def test_empty_first_page_with_total_records_no_position(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Пустая первая страница при `total` > 0: позиции нет (не «0+»), цикл не падает."""
    client = FakeKaspiClient()
    client.offers_page = OffersPage(offers=[], total=30)
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True, client=client)
    await run_one_slice(p.ctx, p.database)
    assert await fetch(rig, OBSERVED) == [(100, None, None, 30, 100)]
    assert await journal(rig, p.shop.shop_id) == [
        (100, 100, "own_offer_missing", "no_change", None)
    ]
