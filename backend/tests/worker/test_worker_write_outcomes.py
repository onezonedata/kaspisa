"""Т34–Т37: исходы `set_price` на настоящем клиенте и учёт отказов."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    Sleeps,
    execute,
    fetch,
    journal,
    listed_product,
    make_worker_ctx,
    open_slice_ctx,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import decision, hold_lease, prepare

from kaspisa.kaspi import (
    KaspiClient,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.kaspi.models import ListedProduct
from kaspisa.worker.constants import UNBOUNDED_RECHECK_SECONDS

FIXTURE = (
    Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi" / "null-stock-product.json"
)
SKU = "900000005_900000024"
MC = "https://mc.shop.kaspi.kz"
EDIT_URL = f"{MC}/bff/offer-view/edit-price"
LIST_URL = f"{MC}/bff/offer-view/v2/list"
WRITE_URL = f"{MC}/pricefeed/upload/merchant/process"
LISTED_PRICE = 2998
FIXTURE_CITY = "710000000"


def _sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


# --- Т34: исходы записи на настоящем KaspiClient ---


async def _slow_reply(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(2)
    return httpx.Response(200, json={"id": "late"})


def _post(**kwargs: Any) -> dict[str, Any]:
    return kwargs


CASES: list[tuple[str, dict[str, Any], str]] = [
    (
        "redirect",
        _post(
            return_value=httpx.Response(
                302, headers={"Location": "https://idmc.shop.kaspi.kz/login"}
            )
        ),
        "expired",
    ),
    ("401", _post(return_value=httpx.Response(401)), "expired"),
    ("5xx", _post(return_value=httpx.Response(500)), "unknown"),
    ("transport", _post(side_effect=httpx.ConnectError("x")), "unknown"),
    ("garbage", _post(return_value=httpx.Response(200, content=b"not json")), "unknown"),
    ("400", _post(return_value=httpx.Response(400, json={"error": "x"})), "unknown"),
    ("timeout", _post(side_effect=_slow_reply), "unknown"),
    ("429", _post(return_value=httpx.Response(429, headers={"Retry-After": "7"})), "rate_limited"),
    ("counted_stale", _post(return_value=httpx.Response(200, json={"id": "j"})), "refused"),
]


@pytest.mark.parametrize(("name", "reply", "expect"), CASES, ids=[c[0] for c in CASES])
async def test_set_price_outcomes_on_real_client(
    name: str,
    reply: dict[str, Any],
    expect: str,
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kaspisa.worker import writer
    from kaspisa.worker.errors import ShopThrottled
    from kaspisa.worker.writer import WriteOutcome, write_price

    monkeypatch.setattr(writer, "SET_PRICE_TIMEOUT_SECONDS", 0.2)
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, skus=[SKU])
    await hold_lease(rig, shop, "w1", NOW + timedelta(days=1))
    await execute(
        rig,
        "UPDATE product SET min_price=100, max_price=5000",
    )
    await execute(rig, "UPDATE shop SET city_id=:c", c=FIXTURE_CITY)  # город сверки из примера
    sample = _sample()
    listing_row = copy.deepcopy(sample["listRow"])
    if name == "counted_stale":  # остаток указан, в чтении не указан (этап 23, Т9)
        listing_row["availabilities"][0].update(stockCount=5, stockSpecified=True)
    ctx0 = make_worker_ctx(database, None, redis_client, redis_prefix, clock)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        mock.get(url__startswith=EDIT_URL).mock(
            return_value=httpx.Response(200, json=sample["editPrice"])
        )
        mock.post(url__startswith=LIST_URL).mock(
            return_value=httpx.Response(201, json={"data": [listing_row], "total": 1})
        )
        write_route = mock.post(WRITE_URL).mock(**reply)
        async with KaspiClient(limiter=ctx0.limiter, sleep=Sleeps()) as real:
            ctx = replace(ctx0, client=real)
            sctx = await open_slice_ctx(ctx, shop)
            async with database.tenant(shop.tenant_id) as scope:
                product = await scope.products.get(shop.product_ids[0])
            assert product is not None
            call = write_price(sctx, product, decision(2900, 2950), LISTED_PRICE)
            if expect == "rate_limited":
                with pytest.raises(ShopThrottled):
                    await call
            else:
                result = await call
    writes = await fetch(rig, "SELECT status FROM price_write")
    rows = await journal(rig, shop.shop_id)
    streak = (await shop_row(rig, shop.shop_id))["write_streak"]
    state = (await shop_row(rig, shop.shop_id))["state"]
    if expect in ("expired", "unknown"):
        assert writes == [("unknown",)]
        assert [r[3] for r in rows] == ["unconfirmed"]
        assert streak == (0 if expect == "expired" else 1)
        assert state == ("reauth_required" if expect == "expired" else "connected")
    else:
        assert writes == []
        assert [r[3] for r in rows] == ([] if expect == "rate_limited" else ["failed"])
        assert streak == 0
        if expect == "refused":
            assert result.outcome is WriteOutcome.REFUSED
            assert write_route.call_count == 0
            if name == "counted_stale":
                assert [r[4] for r in rows] == ["stale_listing"]
    if expect != "refused" and name != "timeout":  # при тайм-ауте запрос оборван до ответа
        assert write_route.call_count >= 1


async def test_limiter_failure_during_set_price_is_compensated(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.redis_limits import LimiterUnavailableError, RedisIntervalLimiter
    from kaspisa.worker.writer import write_price

    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, skus=[SKU])
    await hold_lease(rig, shop, "w1", NOW + timedelta(days=1))
    await execute(rig, "UPDATE product SET min_price=100, max_price=5000")
    await execute(rig, "UPDATE shop SET city_id=:c", c=FIXTURE_CITY)
    sample = _sample()

    class Flaky(RedisIntervalLimiter):
        def __init__(self) -> None:
            super().__init__(redis_client, prefix=redis_prefix, clock=clock.seconds, sleep=Sleeps())
            self.mc_calls = 0

        async def acquire(self, key: str) -> None:
            if key.startswith("mc:"):
                self.mc_calls += 1
                if self.mc_calls == 3:  # get_product, find_product, затем запись
                    self.failures += 1
                    raise LimiterUnavailableError("ConnectionError")
            await super().acquire(key)

    limiter = Flaky()
    ctx0 = make_worker_ctx(database, None, redis_client, redis_prefix, clock)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        mock.get(url__startswith=EDIT_URL).mock(
            return_value=httpx.Response(200, json=sample["editPrice"])
        )
        mock.post(url__startswith=LIST_URL).mock(
            return_value=httpx.Response(201, json={"data": [sample["listRow"]], "total": 1})
        )
        route = mock.post(WRITE_URL).mock(return_value=httpx.Response(200, json={"id": "j"}))
        async with KaspiClient(limiter=limiter, sleep=Sleeps()) as real:
            ctx = replace(ctx0, client=real, limiter=limiter)
            sctx = await open_slice_ctx(ctx, shop)
            async with database.tenant(shop.tenant_id) as scope:
                product = await scope.products.get(shop.product_ids[0])
            assert product is not None
            with pytest.raises(LimiterUnavailableError):
                await write_price(
                    sctx,
                    product,
                    decision(2900, 2950),
                    LISTED_PRICE,
                )
    assert route.call_count == 0
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await journal(rig, shop.shop_id) == []
    assert (await shop_row(rig, shop.shop_id))["write_streak"] == 0


async def test_failed_compensation_keeps_hold_and_streak(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kaspisa.db.repositories import PriceWriteRepository

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.PREORDER, p.sku)

    async def broken(self: PriceWriteRepository, product_id: int) -> bool:
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(PriceWriteRepository, "delete", broken)
    await p.write(94)
    assert len(await fetch(rig, "SELECT 1 FROM price_write")) == 1
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0


# --- Т35–Т37: отказы ---

TRANSIENT = [WriteRefusal.STALE_LISTING, WriteRefusal.STORES_CHANGED, WriteRefusal.PRICE_MISMATCH]
OTHER = [
    WriteRefusal.UNVERIFIED_STORE_STATE,
    WriteRefusal.PREORDER,
    WriteRefusal.NO_STORES,
    WriteRefusal.ARCHIVED,
    WriteRefusal.MODEL_MISSING,
    WriteRefusal.INVALID_PRICES,
]


async def _refusal(rig: Rig, product_id: int) -> tuple[Any, ...]:
    rows = await fetch(
        rig,
        "SELECT refusal_reason, refusal_since, retry_at FROM product WHERE id=:i",
        i=product_id,
    )
    return rows[0]


@pytest.mark.parametrize("reason", TRANSIENT)
async def test_transient_refusal_becomes_permanent_after_300_seconds(
    reason: WriteRefusal, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(reason, p.sku)
    await p.write(94)  # since = T
    assert (await _refusal(rig, p.product_id))[:3] == (reason.value, NOW, None)
    p.clock.now = NOW + timedelta(seconds=300)
    await p.write(94)
    assert (await _refusal(rig, p.product_id))[2] is None  # ровно 300 с: ещё не постоянный
    p.clock.now = NOW + timedelta(seconds=301)
    await p.write(94)
    retry_at = NOW + timedelta(seconds=301 + 1800)
    assert (await _refusal(rig, p.product_id))[0::2] == (f"permanent:{reason.value}", retry_at)
    await p.write(94)  # повтор постоянного отказа не добавляет строк
    assert [r[4] for r in await journal(rig, p.shop.shop_id)] == [
        reason.value,
        f"permanent:{reason.value}",
    ]
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0
    async with p.database.tenant(p.shop.tenant_id) as scope:
        early = retry_at - timedelta(seconds=1)
        assert (
            await scope.products.next_due(
                p.shop.shop_id,
                now=early,
                recheck_seconds=60,
                unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
                bounded=True,
            )
            is None
        )
        assert await scope.products.next_due(
            p.shop.shop_id,
            now=retry_at,
            recheck_seconds=60,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
            bounded=True,
        )


async def test_sent_row_after_refusal_restarts_the_series(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STALE_LISTING, p.sku)
    await p.write(94)
    await execute(
        rig,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, created_at, old_price, "
        "new_price, reason, result) VALUES (:t, :s, :p, :c, 100, 90, 'x', 'unconfirmed')",
        t=p.shop.tenant_id,
        s=p.shop.shop_id,
        p=p.product_id,
        c=NOW + timedelta(seconds=100),
    )
    p.clock.now = NOW + timedelta(seconds=301)
    await p.write(94)
    reason, since, retry_at = await _refusal(rig, p.product_id)
    assert (reason, since, retry_at) == ("stale_listing", NOW + timedelta(seconds=301), None)


@pytest.mark.parametrize(
    ("reason", "change"),
    [(r, "stock") for r in OTHER]
    + [(WriteRefusal.PREORDER, "min"), (WriteRefusal.PREORDER, "max")],
)
async def test_other_refusals_wait_for_changed_data(
    reason: WriteRefusal,
    change: str,
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(reason, p.sku)
    await run_one_slice(p.ctx, p.database)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "failed", reason.value)
    ]
    full = ["find_product", "get_offers", "get_product", "find_product", "set_price"]
    assert p.client.methods() == full
    for _ in range(2):  # тот же отпечаток: только строка списка
        p.clock.advance(61)
        await run_one_slice(p.ctx, p.database)
    assert p.client.methods() == [*full, "find_product", "find_product"]
    if change == "stock":
        p.client.stock_count = 3
    elif change == "min":
        await execute(rig, "UPDATE product SET min_price=60 WHERE id=:i", i=p.product_id)
    else:
        await execute(rig, "UPDATE product SET max_price=400 WHERE id=:i", i=p.product_id)
    p.clock.advance(61)
    await run_one_slice(p.ctx, p.database)
    assert p.client.methods()[-5:] == full  # попытка снова
    assert len(await journal(rig, p.shop.shop_id)) == 1  # причина та же: строки нет


async def test_refusal_journal_is_not_duplicated(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STALE_LISTING, p.sku)
    for _ in range(5):
        await p.write(94)
    rows = await journal(rig, p.shop.shop_id)
    assert [(r[3], r[4]) for r in rows] == [("failed", "stale_listing")]
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STOCK_COUNTED, p.sku)
    await p.write(94)
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.PREORDER, p.sku)
    await p.write(94)
    assert [r[4] for r in await journal(rig, p.shop.shop_id)] == [
        "stale_listing",
        "stock_counted",
        "preorder",
    ]
