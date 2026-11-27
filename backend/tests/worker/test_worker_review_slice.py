"""Т73, Т74: квота не мешает подтверждениям; непредвиденная ошибка слайса откладывает магазин."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

from datetime import datetime, timedelta

from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    execute,
    journal,
    not_applied,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, add_sent_rows, prepare


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


async def _slice_at(p: Prepared, seconds: int) -> bool:
    p.clock.now = at(seconds)
    return await run_one_slice(p.ctx, p.database, at(seconds))


# --- Т73 ---


async def test_exhausted_quota_still_verifies_open_writes(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    assert (await p.write(94)).outcome.value == "sent"
    await add_sent_rows(rig, p.shop, 250, NOW)
    # слайс в момент отправки: решений нет, магазин отложен до ближайшей проверки
    assert await _slice_at(p, 0)
    assert p.client.methods() == ["get_product", "find_product", "set_price"]  # решений нет
    row = await shop_row(rig, p.shop.shop_id)
    assert row["claimed_until"] == at(30)  # min(quota_free_at = +1800, next_verify_at = +30)
    assert not await _slice_at(p, 29)
    calls = len(p.client.calls)
    assert await _slice_at(p, 30)  # магазин отдан только для подтверждения
    assert p.client.calls[calls:] == [("verify_price", p.sku)]
    assert (await journal(rig, p.shop.shop_id))[0][3] == "applied"
    assert (await shop_row(rig, p.shop.shop_id))["claimed_until"] == at(1800)  # квота


async def test_unconfirmed_write_under_exhausted_quota_follows_general_rules(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    assert (await p.write(94)).outcome.value == "sent"
    await add_sent_rows(rig, p.shop, 250, NOW)
    p.client.verify_results = [not_applied() for _ in range(12)]
    for t in range(30, 301, 30):
        assert await _slice_at(p, t), t
    assert p.client.methods().count("verify_price") == 10
    rows = await journal(rig, p.shop.shop_id)
    assert rows[0][3:] == ("unconfirmed", "not_confirmed")
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert (await shop_row(rig, p.shop.shop_id))["claimed_until"] == at(600)


# --- Т74 ---


async def test_unexpected_error_defers_the_shop_for_60_seconds(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    assert (await p.write(94)).outcome.value == "sent"
    await execute(
        rig,
        "UPDATE price_write SET snapshot='{\"broken\": true}'::jsonb WHERE shop_id=:s",
        s=p.shop.shop_id,
    )
    healthy = await seed_shop(rig, p.database, products=1)
    calls = len(p.client.calls)
    while await _slice_at(p, 30):  # процесс жив, остальные магазины обработаны
        pass
    broken = await shop_row(rig, p.shop.shop_id)
    assert (broken["claimed_by"], broken["claimed_until"]) == ("w1", at(90))
    assert ("find_product", healthy.skus[0]) in p.client.calls[calls:]
    while await _slice_at(p, 89):  # здоровый магазин проверяет свою запись, испорченный ждёт
        pass
    assert (await shop_row(rig, p.shop.shop_id))["claimed_until"] == at(90)
    while await _slice_at(p, 90):
        pass
    assert (await shop_row(rig, p.shop.shop_id))["claimed_until"] == at(150)
