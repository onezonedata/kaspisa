"""ADR 0030, Т9-Т11, Т13-Т16, Т46: товары без границ - цены конкурентов, решение, порядок слайса."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    CITY_ID,
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    execute,
    fetch,
    make_worker_ctx,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, hold_lease, prepare

from kaspisa.kaspi import SessionExpiredError
from kaspisa.kaspi.models import Offer, OffersPage
from kaspisa.worker import constants
from kaspisa.worker import slice as slice_module
from kaspisa.worker.cycle import check_product

FIXTURE = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi" / "offers.json"
LEASE_UNTIL = NOW + timedelta(days=3650)


def _page(own_uid: str, prices: dict[str, int] | None = None) -> OffersPage:
    """Карточка из `offers.json`: продавец 90100001 - свой магазин; `prices` меняет цены."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    offers = []
    for raw in data["offers"]:
        if raw["merchantId"] == "90100001":
            raw["merchantId"] = own_uid
        if prices and raw["merchantId"] in prices:
            raw["price"] = prices[raw["merchantId"]]
        offers.append(Offer.model_validate(raw))
    return OffersPage(offers=offers, total=data["total"])


async def _unbounded(
    rig: Rig,
    redis: Redis,
    prefix: str,
    *,
    low: int | None = None,
    high: int | None = None,
    bot_mode: str = "dry_run",
    dry_run: bool = True,
) -> Prepared:
    """Магазин с одним отслеживаемым товаром без границ, своя цена 2998, карточка из фикстуры."""
    database = rig.new_database()
    shop = await seed_shop(rig, database, bot_mode=bot_mode)
    p = await prepare(rig, redis, prefix, dry_run=dry_run, database=database, shop=shop)
    p.client.price = 2998
    p.client.offers_page = _page(shop.merchant_uid)
    await execute(
        rig,
        "UPDATE product SET min_price=:lo, max_price=:hi WHERE id=:i",
        lo=low,
        hi=high,
        i=p.product_id,
    )
    return p


async def _check(p: Prepared) -> Any:
    p.clock.advance(61)
    await hold_lease(p.rig, p.shop, "w1", LEASE_UNTIL)
    return await check_product(p.sctx, await p.product())


async def _row(rig: Rig, product_id: int) -> dict[str, Any]:
    rows = await fetch(
        rig,
        "SELECT current_price, position, position_over, competitors_count, first_place_price, "
        "decision_reason, decision_target_price, decision_write, skip_reason, last_checked_at "
        "FROM product WHERE id=:i",
        i=product_id,
    )
    keys = [
        "current_price",
        "position",
        "position_over",
        "competitors_count",
        "first_place_price",
        "decision_reason",
        "decision_target_price",
        "decision_write",
        "skip_reason",
        "last_checked_at",
    ]
    return dict(zip(keys, rows[0], strict=True))


async def _journal(rig: Rig, product_id: int) -> list[tuple[Any, ...]]:
    """(result, reason, old, new, target, competitor_merchant_id, competitor_price)."""
    return await fetch(
        rig,
        "SELECT result, reason, old_price, new_price, target_price, competitor_merchant_id, "
        "competitor_price FROM price_change WHERE product_id=:i ORDER BY id",
        i=product_id,
    )


def _numbers(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["current_price"],
        row["position"],
        row["position_over"],
        row["competitors_count"],
        row["first_place_price"],
        row["decision_reason"],
        row["decision_target_price"],
        row["decision_write"],
        row["skip_reason"],
    )


T9_NUMBERS = (2998, 3, None, 6, 2643, "bounds_missing", None, None, None)
T9_JOURNAL = ("no_change", "bounds_missing", 2998, 2998, None, "90100003", 2643)


def _no_write(p: Prepared) -> None:
    methods = p.client.methods()
    assert "set_price" not in methods
    assert "get_product" not in methods
    assert p.client.sent == []


# --- Т9 ---


async def test_unbounded_product_observed_with_bounds_missing(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _unbounded(rig, redis_client, redis_prefix)
    result = await _check(p)
    assert result is None
    assert _numbers(await _row(rig, p.product_id)) == T9_NUMBERS
    assert await _journal(rig, p.product_id) == [T9_JOURNAL]
    _no_write(p)
    assert await fetch(rig, "SELECT 1 FROM price_write WHERE shop_id=:s", s=p.shop.shop_id) == []


# --- Т10 ---


async def test_unbounded_journal_follows_key(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _unbounded(rig, redis_client, redis_prefix)
    uid = p.shop.merchant_uid
    await _check(p)
    await _check(p)  # те же цифры: строки не прибавляется
    assert await _journal(rig, p.product_id) == [T9_JOURNAL]

    p.client.offers_page = _page(uid, {"90100003": 2600})
    await _check(p)
    rows = await _journal(rig, p.product_id)
    assert len(rows) == 2
    assert rows[-1][5:] == ("90100003", 2600)
    row = await _row(rig, p.product_id)
    assert (row["first_place_price"], row["position"]) == (2600, 3)

    p.sctx.shop.excluded_merchant_ids = ["90100003"]
    await _check(p)
    rows = await _journal(rig, p.product_id)
    assert len(rows) == 3
    assert rows[-1][5:] == ("90100005", 2643)
    row = await _row(rig, p.product_id)
    assert (row["position"], row["first_place_price"]) == (3, 2600)  # ADR 0016
    _no_write(p)


# --- Т11 ---


@pytest.mark.parametrize(("low", "high"), [(100, None), (None, 5000)])
async def test_only_one_bound_counts_as_unbounded(
    low: int | None, high: int | None, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _unbounded(rig, redis_client, redis_prefix, low=low, high=high)
    await _check(p)
    assert _numbers(await _row(rig, p.product_id)) == T9_NUMBERS
    assert await _journal(rig, p.product_id) == [T9_JOURNAL]
    _no_write(p)


# --- Т13 ---


async def test_bounds_set_during_unbounded_check_writes_nothing(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _unbounded(rig, redis_client, redis_prefix)

    async def set_bounds(method: str, key: str) -> None:
        if method == "get_offers":
            await execute(
                rig, "UPDATE product SET min_price=100, max_price=5000 WHERE id=:i", i=p.product_id
            )

    p.client.on_call = set_bounds
    await _check(p)
    row = await _row(rig, p.product_id)
    assert row["decision_reason"] is None
    assert row["current_price"] is None  # финиш вышел до записи наблюдения
    assert await _journal(rig, p.product_id) == []
    _no_write(p)


# --- Т14 ---


async def test_unbounded_skip_paths(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    p = await _unbounded(rig, redis_client, redis_prefix)
    p.client.find_results = [None]
    await _check(p)
    row = await _row(rig, p.product_id)
    assert (row["skip_reason"], row["decision_reason"]) == ("listing_missing", None)
    assert await _journal(rig, p.product_id) == []

    p.client.cities = {"111": 2998, "222": 3100}  # города сверки среди них нет
    await _check(p)
    row = await _row(rig, p.product_id)
    assert (row["skip_reason"], row["decision_reason"]) == ("own_price_unknown", None)
    assert await _journal(rig, p.product_id) == []
    assert "get_offers" not in p.client.methods()


# --- Т15 ---


async def _fresh_shop_with_two_products(
    rig: Rig, redis: Redis, prefix: str
) -> tuple[Any, FakeKaspiClient, FakeClock, Any]:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=2, bot_mode="off")
    client = FakeKaspiClient(price=2998, clock=clock)
    client.offers_page = _page(shop.merchant_uid)
    ctx = make_worker_ctx(database, client, redis, prefix, clock, dry_run=False)
    await execute(
        rig,
        "UPDATE product SET min_price=NULL, max_price=NULL WHERE id=:i",
        i=shop.product_ids[0],
    )
    return shop, client, clock, (ctx, database)


async def test_fresh_shop_bot_off_reads_nothing(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т50 (ADR 0031): при режиме «выключен» не проверяется ни один товар; в `dry_run` - да."""
    shop, client, _, (ctx, database) = await _fresh_shop_with_two_products(
        rig, redis_client, redis_prefix
    )
    await execute(
        rig,
        "UPDATE product SET min_price=100, max_price=5000 WHERE id=:i",
        i=shop.product_ids[1],
    )
    a, b = shop.product_ids
    await run_one_slice(ctx, database)  # магазин может быть захвачен ради каталога
    for product_id in (a, b):
        row = await _row(rig, product_id)
        assert row["last_checked_at"] is None
        assert row["decision_reason"] is None
        assert row["current_price"] is None
        assert row["position"] is None
    assert not [m for m in client.methods() if m in {"find_product", "get_offers"}]
    assert "set_price" not in client.methods()

    await execute(rig, "UPDATE shop SET bot_mode='dry_run' WHERE id=:i", i=shop.shop_id)
    assert await run_one_slice(ctx, database) is True
    assert _numbers(await _row(rig, a)) == T9_NUMBERS


@pytest.mark.parametrize(
    "update",
    [
        "halt_reason='write_errors', halted_at=now()",
        "connection_state='reauth_required'",
        "connection_state='reauth_required', connection_error='session_expired'",
    ],
)
async def test_fresh_shop_not_claimed_for_unbounded_when_stopped_or_needs_login(
    update: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    shop, client, _, (ctx, database) = await _fresh_shop_with_two_products(
        rig, redis_client, redis_prefix
    )
    await execute(rig, f"UPDATE shop SET {update} WHERE id=:i", i=shop.shop_id)
    assert await run_one_slice(ctx, database) is False
    assert client.calls == []


# --- Т16 ---


async def test_unbounded_rechecked_every_600_seconds(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=2, bot_mode="dry_run")
    unbounded, bounded = shop.skus
    await execute(
        rig,
        "UPDATE product SET min_price=NULL, max_price=NULL WHERE id=:i",
        i=shop.product_ids[0],
    )
    client = FakeKaspiClient(price=2998, clock=clock)
    client.offers_page = _page(shop.merchant_uid)
    ctx = make_worker_ctx(database, client, redis_client, redis_prefix, clock, dry_run=True)

    def checked() -> list[str]:
        found = [key for method, key in client.calls if method == "find_product"]
        client.calls.clear()
        return found

    assert await run_one_slice(ctx, database) is True
    assert sorted(checked()) == sorted([unbounded, bounded])
    clock.advance(60)
    assert await run_one_slice(ctx, database) is True
    assert checked() == [bounded]
    clock.advance(540)
    assert await run_one_slice(ctx, database) is True
    assert sorted(checked()) == sorted([unbounded, bounded])


# --- Т46 ---


async def _mixed_shop(
    rig: Rig, redis: Redis, prefix: str, *, unbounded: int, bounded: int, bot_mode: str
) -> tuple[Any, FakeKaspiClient, Any, Any, list[str], list[str]]:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, products=unbounded + bounded, bot_mode=bot_mode)
    await execute(
        rig,
        "UPDATE product SET min_price=NULL, max_price=NULL WHERE id = ANY(:ids)",
        ids=shop.product_ids[:unbounded],
    )
    client = FakeKaspiClient(price=100, clock=clock)
    ctx = make_worker_ctx(database, client, redis, prefix, clock, dry_run=True)
    return shop, client, ctx, database, shop.skus[:unbounded], shop.skus[unbounded:]


def _found(client: FakeKaspiClient) -> list[str]:
    found = [key for method, key in client.calls if method == "find_product"]
    client.calls.clear()
    return found


async def test_bounded_first_one_unbounded_per_slice(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (а) первый слайс: U1, затем все с границами, затем остальные без границ
    _, client, ctx, database, unb, bnd = await _mixed_shop(
        rig, redis_client, redis_prefix, unbounded=100, bounded=5, bot_mode="dry_run"
    )
    assert await run_one_slice(ctx, database) is True
    found = _found(client)
    assert found[0] == unb[0]
    assert found[1:6] == bnd
    assert found[6:] == unb[1 : 1 + len(found) - 6]
    assert len(found) == constants.SLICE_MAX_PRODUCTS


async def test_bounded_always_due_still_one_unbounded_per_slice(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (б) товары с границами должны всегда: в каждом слайсе один без границ, и он первый
    monkeypatch.setattr(constants, "MIN_RECHECK_SECONDS", 0)
    monkeypatch.setattr(slice_module, "MIN_RECHECK_SECONDS", 0)
    _, client, ctx, database, unb, _ = await _mixed_shop(
        rig, redis_client, redis_prefix, unbounded=3, bounded=2, bot_mode="dry_run"
    )
    for index in range(3):
        assert await run_one_slice(ctx, database) is True
        found = _found(client)
        assert found[0] == unb[index]
        assert [sku for sku in found if sku in unb] == [unb[index]]


async def test_bot_off_slice_checks_nothing(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т51 (ADR 0031): слайс при режиме «выключен» не проверяет ни товары без границ, ни с ними."""
    from kaspisa.db import ClaimedShop
    from kaspisa.worker.slice import run_shop_slice

    shop, client, ctx, _database, _, _ = await _mixed_shop(
        rig, redis_client, redis_prefix, unbounded=3, bounded=2, bot_mode="off"
    )
    await hold_lease(rig, shop, "w1", LEASE_UNTIL)  # прямой слайс без захвата
    await run_shop_slice(ctx, ClaimedShop(tenant_id=shop.tenant_id, shop_id=shop.shop_id))
    assert not [m for m in client.methods() if m in {"find_product", "get_offers"}]
    rows = await fetch(rig, "SELECT last_checked_at FROM product WHERE shop_id=:s", s=shop.shop_id)
    assert len(rows) == 5
    assert all(row[0] is None for row in rows)
    assert await fetch(rig, "SELECT 1 FROM price_change WHERE shop_id=:s", s=shop.shop_id) == []


async def test_shop_halted_after_claim_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Остановка магазина между захватом и проверкой отключает и чтение цен без границ."""
    from kaspisa.worker.access import ShopAccess

    shop, client, _, (ctx, database) = await _fresh_shop_with_two_products(
        rig, redis_client, redis_prefix
    )
    await execute(rig, "UPDATE shop SET bot_mode='dry_run' WHERE id=:i", i=shop.shop_id)
    original = ShopAccess.refresh

    async def halting_refresh(self: ShopAccess) -> None:
        await execute(
            rig,
            "UPDATE shop SET halt_reason='write_errors', halted_at=now() WHERE id=:i",
            i=shop.shop_id,
        )
        await original(self)

    monkeypatch.setattr(ShopAccess, "refresh", halting_refresh)
    assert await run_one_slice(ctx, database) is True
    assert not [m for m in client.methods() if m in {"find_product", "get_offers"}]
    assert (await _row(rig, shop.product_ids[0]))["last_checked_at"] is None
