"""Т62, Т63: полный цикл товара на настоящем KaspiClient; HTTP подменён `respx`."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import respx
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    Rig,
    Sleeps,
    execute,
    fetch,
    journal,
    make_worker_ctx,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    worker_cipher,
    worker_database_url,
)

from kaspisa.kaspi import KaspiClient

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"
SKU = "900000005_900000024"
MASTER = "900000005"
CITY = "710000000"
MC = "https://mc.shop.kaspi.kz"
EDIT_URL = f"{MC}/bff/offer-view/edit-price"
LIST_URL = f"{MC}/bff/offer-view/v2/list"
WRITE_URL = f"{MC}/pricefeed/upload/merchant/process"
OFFERS_URL = f"https://kaspi.kz/yml/offer-view/offers/{MASTER}"
OWN_PRICE = 2998
COMPETITOR_PRICE = 2643


def _sample() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "null-stock-product.json").read_text(encoding="utf-8")
    )
    return data


def _offers(own_uid: str) -> dict[str, Any]:
    """Продавцы из фикстуры; свой магазин получает uid подготовленного магазина и цену списка."""
    data: dict[str, Any] = json.loads((FIXTURES / "offers.json").read_text(encoding="utf-8"))
    for offer in data["offers"]:
        if offer["merchantId"] == "90100003":
            offer["merchantId"] = own_uid
            offer["price"] = OWN_PRICE
    return data


class Kaspi:
    """Заглушка Kaspi на `respx`: список товаров показывает текущую цену магазина."""

    def __init__(self, mock: respx.MockRouter, own_uid: str) -> None:
        sample = _sample()
        self.price = OWN_PRICE
        self.row = sample["listRow"]

        def listing(request: httpx.Request) -> httpx.Response:
            row = copy.deepcopy(self.row)
            row["cityPrices"][0]["value"] = self.price
            return httpx.Response(201, json={"data": [row], "total": 1})

        self.list = mock.post(url__startswith=LIST_URL).mock(side_effect=listing)
        self.edit = mock.get(url__startswith=EDIT_URL).mock(
            return_value=httpx.Response(200, json=sample["editPrice"])
        )
        self.offers = mock.post(OFFERS_URL).mock(
            return_value=httpx.Response(200, json=_offers(own_uid))
        )
        self.write = mock.post(WRITE_URL).mock(
            return_value=httpx.Response(200, json=sample["priceUpdateResponse"]["body"])
        )


async def _scenario(
    rig: Rig, redis: Redis, prefix: str, *, dry_run: bool
) -> tuple[Any, Any, Any, Any, None]:
    clock = FakeClock()
    database = rig.new_database()
    shop = await seed_shop(rig, database, skus=[SKU])
    await execute(
        rig,
        "UPDATE product SET master_sku=:m, min_price=2000, max_price=5000",
        m=MASTER,
    )
    await execute(rig, "UPDATE shop SET city_id=:c WHERE id=:i", c=CITY, i=shop.shop_id)
    ctx = make_worker_ctx(database, None, redis, prefix, clock, dry_run=dry_run)
    return clock, database, shop, ctx, None


# --- Т62 ---


async def test_full_cycle_with_writing(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    clock, database, shop, ctx0, _ = await _scenario(rig, redis_client, redis_prefix, dry_run=False)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        kaspi = Kaspi(mock, shop.merchant_uid)
        async with KaspiClient(limiter=ctx0.limiter, sleep=Sleeps()) as client:
            ctx = replace(ctx0, client=client)
            assert await run_one_slice(ctx, database, NOW)
            # решение принято и цена отправлена ровно один раз
            assert kaspi.offers.call_count == 1
            assert kaspi.edit.call_count == 1
            assert kaspi.list.call_count == 2  # строка списка до решения и свежая перед записью
            assert kaspi.write.call_count == 1
            sent = json.loads(kaspi.write.calls[0].request.content)
            ((_old, target, _reason, result, error),) = await journal(rig, shop.shop_id)
            assert (result, error) == ("unconfirmed", None)
            assert COMPETITOR_PRICE - 10 < target < OWN_PRICE
            assert sent["cityPrices"] == [{"cityId": CITY, "value": target}]
            ((checked, price, position, rivals),) = await fetch(
                rig,
                "SELECT last_checked_at, current_price, position, competitors_count FROM product",
            )
            assert (checked, price) == (NOW, OWN_PRICE)
            assert position is not None and rivals is not None
            # Kaspi применил цену; через 30 с подтверждение переводит журнал в applied
            kaspi.price = target
            clock.now = NOW + timedelta(seconds=30)
            assert await run_one_slice(ctx, database, clock())
            assert (await journal(rig, shop.shop_id))[0][3:] == ("applied", None)
            ((status,),) = await fetch(rig, "SELECT status FROM price_write")
            assert status == "confirmed"
            # запрет снят, но новых записей нет: товар уже на целевой цене
            clock.now = NOW + timedelta(seconds=120)
            await run_one_slice(ctx, database, clock())
            assert kaspi.write.call_count == 1
    assert (await shop_row(rig, shop.shop_id))["write_streak"] == 0


# --- Т63 ---


async def test_full_cycle_in_dry_run_never_writes(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    _clock, database, shop, ctx0, _ = await _scenario(rig, redis_client, redis_prefix, dry_run=True)
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        kaspi = Kaspi(mock, shop.merchant_uid)
        async with KaspiClient(limiter=ctx0.limiter, sleep=Sleeps()) as client:
            ctx = replace(ctx0, client=client)
            assert await run_one_slice(ctx, database, NOW)
    assert kaspi.write.call_count == 0
    assert kaspi.edit.call_count == 0  # чтение для записи не нужно
    assert kaspi.list.call_count == 1
    assert kaspi.offers.call_count == 1
    rows = await journal(rig, shop.shop_id)
    assert [r[3] for r in rows] == ["dry_run"]
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    ((checked,),) = await fetch(rig, "SELECT last_checked_at FROM product")
    assert checked == NOW
