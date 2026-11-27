"""ADR 0020, Т44-Т49, Т65-Т71: решение каждой проверки в товаре и строки журнала `no_change`."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    Rig,
    execute,
    fetch,
    redis_client,
    redis_prefix,
    rig,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, add_sent_rows, hold_lease, prepare

from kaspisa.db import TenantId
from kaspisa.kaspi import (
    PriceWriteOutcomeUnknownError,
    SessionStateError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.pricing import Decision, Offer, Reason
from kaspisa.worker.cycle import check_product
from kaspisa.worker.errors import ShopUnavailable
from kaspisa.worker.writer import journal_no_change

LEASE_UNTIL = NOW + timedelta(days=3650)


async def _configure(
    p: Prepared,
    *,
    low: int = 600,
    high: int = 800,
    auto_decrease: bool = True,
    auto_raise: bool = True,
) -> None:
    await execute(
        p.rig,
        "UPDATE product SET min_price=:lo, max_price=:hi, auto_decrease=:d, auto_raise=:r "
        "WHERE id=:i",
        lo=low,
        hi=high,
        d=auto_decrease,
        r=auto_raise,
        i=p.product_id,
    )


async def _check(p: Prepared, *, after: int = 61) -> Any:
    """Один цикл товара; время между циклами идёт вперёд."""
    p.clock.advance(after)
    await hold_lease(p.rig, p.shop, "w1", LEASE_UNTIL)
    return await check_product(p.sctx, await p.product())


async def _rows(p: Prepared) -> list[tuple[Any, ...]]:
    """Журнал товара: (result, reason, old, new, target, competitor_price)."""
    return await fetch(
        p.rig,
        "SELECT result, reason, old_price, new_price, target_price, competitor_price "
        "FROM price_change WHERE product_id=:i ORDER BY id",
        i=p.product_id,
    )


async def _decision(p: Prepared) -> tuple[Any, ...]:
    rows = await fetch(
        p.rig,
        "SELECT decision_reason, decision_target_price, decision_write, skip_reason, "
        "last_checked_at FROM product WHERE id=:i",
        i=p.product_id,
    )
    return rows[0]


def _no_write_calls(p: Prepared) -> None:
    methods = p.client.methods()
    assert methods.count("get_product") == 0
    assert methods.count("set_price") == 0


# --- Т44, Т45 ---


@pytest.mark.parametrize("dry_run", [True, False])
async def test_out_of_bounds_writes_one_row_and_never_a_price(
    dry_run: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=dry_run)
    p.client.price, p.client.competitor_price = 590, 700
    await _configure(p)
    for _ in range(3):
        await _check(p)
    assert await _rows(p) == [("no_change", "out_of_bounds", 590, 590, None, None)]
    assert await _decision(p) == ("out_of_bounds", None, None, None, p.clock.now)
    _no_write_calls(p)


@pytest.mark.parametrize("dry_run", [True, False])
async def test_edit_then_new_reason_adds_a_row(
    dry_run: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=dry_run)
    p.client.price, p.client.competitor_price = 590, 700
    await _configure(p, auto_raise=False)
    await _check(p)
    assert await _rows(p) == [("no_change", "out_of_bounds", 590, 590, None, None)]
    # правка товара через API: границы меняются, решение обнуляется
    await execute(
        rig,
        "UPDATE product SET min_price=500, decision_reason=NULL, decision_target_price=NULL, "
        "decision_write=NULL WHERE id=:i",
        i=p.product_id,
    )
    await _check(p)
    assert await _rows(p) == [
        ("no_change", "out_of_bounds", 590, 590, None, None),
        ("no_change", "raise_disabled", 590, 590, 699, 700),
    ]
    assert (await _decision(p))[:3] == ("raise_disabled", 699, None)
    _no_write_calls(p)


async def test_check_after_button_read_adds_no_journal_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т72 (часть обработчика): «Узнать цены» сбросила решение, строк журнала не добавила."""
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 650, 599
    await _configure(p, auto_decrease=False)
    await _check(p)
    rows = await _rows(p)
    assert rows == [("no_change", "decrease_disabled", 650, 650, 600, 599)]
    # кнопка: цифры наблюдения записаны, решение сброшено, журнал не тронут
    await execute(
        rig,
        "UPDATE product SET current_price=650, last_checked_at=:t, decision_reason=NULL, "
        "decision_target_price=NULL, decision_write=NULL WHERE id=:i",
        t=p.clock.now,
        i=p.product_id,
    )
    await _check(p)
    assert await _rows(p) == rows
    assert (await _decision(p))[:3] == ("decrease_disabled", 600, None)


# --- Т46 ---


async def test_new_competitor_price_adds_a_row_same_numbers_do_not(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 650, 599
    await _configure(p, auto_decrease=False)
    await _check(p)
    assert await _rows(p) == [("no_change", "decrease_disabled", 650, 650, 600, 599)]
    p.client.competitor_price = 598
    await _check(p)
    await _check(p)
    assert await _rows(p) == [
        ("no_change", "decrease_disabled", 650, 650, 600, 599),
        ("no_change", "decrease_disabled", 650, 650, 600, 598),
    ]


# --- Т47, Т71 ---


async def _last_change(p: Prepared, result: str) -> None:
    await execute(
        p.rig,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, new_price, "
        "reason, target_price, competitor_price, result) VALUES (:t, :s, :p, 605, 600, "
        "'dropped_to_min', 600, 599, :r)",
        t=p.shop.tenant_id,
        s=p.shop.shop_id,
        p=p.product_id,
        r=result,
    )


async def test_at_min_price_after_applied_change_adds_no_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 600, 599
    await _configure(p)
    await _last_change(p, "applied")
    await _check(p)
    assert len(await _rows(p)) == 1
    assert (await _decision(p))[:3] == ("at_min_price", 600, None)


@pytest.mark.parametrize("result", ["dry_run", "failed"])
async def test_at_min_price_after_unsent_change_adds_a_row(
    result: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 600, 599
    await _configure(p)
    await _last_change(p, result)
    await _check(p)
    rows = await _rows(p)
    assert len(rows) == 2
    assert rows[1] == ("no_change", "at_min_price", 600, 600, 600, 599)


# --- Т48, Т49 ---


async def test_own_offer_missing_is_journaled_once(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.own_in_offers = False
    p.client.price = 700
    await _configure(p)
    await _check(p)
    await _check(p)
    assert await _rows(p) == [("no_change", "own_offer_missing", 700, 700, None, None)]
    assert (await _decision(p))[0] == "own_offer_missing"


async def test_listing_missing_resets_decision_and_adds_no_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.price, p.client.competitor_price = 590, 700
    await _configure(p)
    await _check(p)
    assert (await _decision(p))[0] == "out_of_bounds"
    p.client.find_results = [None]
    await _check(p)
    assert len(await _rows(p)) == 1
    reason, target, write, skip, _ = await _decision(p)
    assert (reason, target, write, skip) == (None, None, None, "listing_missing")


# --- Т65-Т69 ---


async def test_dry_run_change_is_stored_with_its_outcome(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    await _check(p)
    assert await _rows(p) == [("dry_run", "undercut_first", 750, 699, 699, 700)]
    assert (await _decision(p))[:4] == ("undercut_first", 699, "dry_run", None)


async def test_quota_outcome_is_stored(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    await add_sent_rows(rig, p.shop, 250, NOW - timedelta(seconds=5))
    await _check(p, after=0)
    assert p.client.methods().count("set_price") == 0
    assert (await _decision(p))[:3] == ("undercut_first", 699, "quota")


async def test_refused_outcome_is_stored(rig: Rig, redis_client: Redis, redis_prefix: str) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STOCK_COUNTED, p.sku)
    await _configure(p)
    await _check(p)
    assert (await _decision(p))[:3] == ("undercut_first", 699, "refused")
    refusal = await fetch(rig, "SELECT refusal_reason FROM product WHERE id=:i", i=p.product_id)
    assert refusal[0][0] is not None


async def test_sent_outcome_then_hold_keeps_decision_and_touches_time(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    await _check(p)
    assert (await _decision(p))[:3] == ("undercut_first", 699, "sent")
    first_check = (await _decision(p))[4]
    await _check(p)  # запись открыта: запрет, цена не читается заново
    reason, target, write, _, checked = await _decision(p)
    assert (reason, target, write) == ("undercut_first", 699, "sent")
    assert checked > first_check
    assert p.client.methods().count("set_price") == 1


async def test_exception_in_write_leaves_decision_empty(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    p.client.errors["set_price"] = SessionStateError("phase")
    await _configure(p)
    with pytest.raises(ShopUnavailable):
        await _check(p)
    assert (await _decision(p))[:4] == (None, None, None, None)


@pytest.mark.parametrize("new_owner", ["decision", "skip"])
async def test_lease_lost_leaves_the_new_owners_product_row_alone(
    new_owner: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Аренду забрали во время записи: итог нового владельца не затирается и не падает."""
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    finds = 0

    async def steal(method: str, key: str) -> None:
        nonlocal finds
        if method != "find_product":
            return
        finds += 1
        if finds < 2:  # первый вызов - чтение строки списка в начале проверки
            return
        await execute(rig, "UPDATE shop SET claimed_by='w2' WHERE id=:s", s=p.shop.shop_id)
        if new_owner == "decision":
            await execute(
                rig,
                "UPDATE product SET decision_reason='undercut_first', "
                "decision_target_price=699, decision_write='sent' WHERE id=:i",
                i=p.product_id,
            )
        else:
            await execute(
                rig,
                "UPDATE product SET skip_reason='listing_missing', skipped_at=:t, "
                "decision_reason=NULL, decision_target_price=NULL, decision_write=NULL "
                "WHERE id=:i",
                t=NOW,
                i=p.product_id,
            )

    p.client.on_call = steal
    p.clock.advance(61)
    await hold_lease(p.rig, p.shop, "w1", LEASE_UNTIL)
    result = await check_product(p.sctx, await p.product())
    assert result is not None and result.reason == "lease_lost"
    assert "set_price" not in p.client.methods()
    reason, target, write, skip, _ = await _decision(p)
    if new_owner == "decision":
        assert (reason, target, write, skip) == ("undercut_first", 699, "sent", None)
    else:
        assert (reason, target, write, skip) == (None, None, None, "listing_missing")


# --- Т70 ---


async def test_parallel_transactions_write_one_no_change_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    verdict = Decision(
        new_price=None,
        reason=Reason.DECREASE_DISABLED,
        target_price=600,
        competitor=Offer(merchant_id="other", price=599),
    )

    async def attempt() -> bool:
        async with p.database.tenant(TenantId(p.shop.tenant_id)) as scope:
            added = await journal_no_change(scope, p.product_id, verdict, 650, "Чужой")
            await scope.commit()
            return added

    results = await asyncio.gather(attempt(), attempt())
    assert sorted(results) == [False, True]
    assert await _rows(p) == [("no_change", "decrease_disabled", 650, 650, 600, 599)]


# --- Дополнительные граничные случаи дедупликации (мутационная проверка) ---


async def test_own_price_change_with_same_reason_adds_a_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 650, 599
    await _configure(p, auto_decrease=False)
    await _check(p)
    p.client.price = 660  # продавец поменял цену в кабинете, причина и цель те же
    await _check(p)
    assert await _rows(p) == [
        ("no_change", "decrease_disabled", 650, 650, 600, 599),
        ("no_change", "decrease_disabled", 660, 660, 600, 599),
    ]


async def test_new_target_with_same_reason_and_competitor_adds_a_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p, auto_decrease=False)
    await _check(p)
    # новый шаг меняет цель (699 -> 698), причина и конкурент те же
    await execute(rig, "UPDATE product SET step=2 WHERE id=:i", i=p.product_id)
    await _check(p)
    assert await _rows(p) == [
        ("no_change", "decrease_disabled", 750, 750, 699, 700),
        ("no_change", "decrease_disabled", 750, 750, 698, 700),
    ]


async def test_already_at_target_after_applied_change_adds_no_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 699, 700
    await _configure(p)
    await execute(
        p.rig,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, new_price, "
        "reason, target_price, competitor_price, result) VALUES (:t, :s, :p, 750, 699, "
        "'undercut_first', 699, 700, 'applied')",
        t=p.shop.tenant_id,
        s=p.shop.shop_id,
        p=p.product_id,
    )
    await _check(p)
    assert len(await _rows(p)) == 1
    assert (await _decision(p))[:3] == ("already_at_target", 699, None)


async def test_second_writer_waits_for_first_and_adds_no_duplicate(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Блокировка товара: второй не читает журнал, пока первый не зафиксировал строку."""
    p = await prepare(rig, redis_client, redis_prefix)
    verdict = Decision(
        new_price=None,
        reason=Reason.DECREASE_DISABLED,
        target_price=600,
        competitor=Offer(merchant_id="other", price=599),
    )
    tenant = TenantId(p.shop.tenant_id)
    async with p.database.tenant(tenant) as first:
        assert await journal_no_change(first, p.product_id, verdict, 650, "Чужой") is True

        async def second() -> bool:
            async with p.database.tenant(tenant) as scope:
                added = await journal_no_change(scope, p.product_id, verdict, 650, "Чужой")
                await scope.commit()
                return added

        waiting = asyncio.create_task(second())
        await asyncio.sleep(0.5)  # второй упёрся в блокировку первого
        assert not waiting.done()
        await first.commit()
    assert await waiting is False
    assert await _rows(p) == [("no_change", "decrease_disabled", 650, 650, 600, 599)]


# --- правка товара между чтением и записью решения (ADR 0020) ---


def _edit_during_offers(p: Prepared, rig: Rig, *, low: int, high: int) -> None:
    """Правка границ через API между чтением товара обработчиком и записью решения."""

    async def edit(method: str, key: str) -> None:
        if method != "get_offers":
            return
        await execute(
            rig,
            "UPDATE product SET min_price=:lo, max_price=:hi, decision_reason=NULL, "
            "decision_target_price=NULL, decision_write=NULL WHERE id=:i",
            lo=low,
            hi=high,
            i=p.product_id,
        )

    p.client.on_call = edit


@pytest.mark.parametrize("edited", [True, False])
async def test_bounds_edit_before_no_change_skips_decision_and_row(
    edited: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.price, p.client.competitor_price = 590, 700
    await _configure(p)
    if edited:
        _edit_during_offers(p, rig, low=500, high=800)
    await _check(p)
    reason, target, write, _, _ = await _decision(p)
    if edited:
        assert (reason, target, write) == (None, None, None)
        assert await _rows(p) == []
    else:
        assert reason == "out_of_bounds"
        assert len(await _rows(p)) == 1


@pytest.mark.parametrize("edited", [True, False])
async def test_bounds_edit_before_dry_run_decision_keeps_row_but_not_decision(
    edited: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    if edited:
        _edit_during_offers(p, rig, low=600, high=900)
    await _check(p)
    reason, target, write, _, _ = await _decision(p)
    assert len(await _rows(p)) == 1  # строка журнала о записи остаётся как есть
    if edited:
        assert (reason, target, write) == (None, None, None)
    else:
        assert (reason, target, write) == ("undercut_first", 699, "dry_run")


# --- каждая настройка товара по отдельности и итоги записи (ADR 0020) ---

# правка по одному полю: у каждого значение отличается от исходного после `_configure`
_FIELD_EDITS = {
    "min_price": "min_price=599",
    "max_price": "max_price=801",
    "step": "step=2",
    "tracked": "tracked=false",
    "auto_decrease": "auto_decrease=false",
    "auto_raise": "auto_raise=false",
}


def _edit_field_during_offers(p: Prepared, rig: Rig, assignment: str) -> None:
    async def edit(method: str, key: str) -> None:
        if method == "get_offers":
            await execute(rig, f"UPDATE product SET {assignment} WHERE id=:i", i=p.product_id)

    p.client.on_call = edit


@pytest.mark.parametrize("field", list(_FIELD_EDITS))
async def test_any_single_setting_edit_skips_no_change_decision_and_row(
    field: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.price, p.client.competitor_price = 590, 700
    await _configure(p)
    _edit_field_during_offers(p, rig, _FIELD_EDITS[field])
    await _check(p)
    assert (await _decision(p))[:3] == (None, None, None)
    assert await _rows(p) == []


@pytest.mark.parametrize("field", list(_FIELD_EDITS))
async def test_any_single_setting_edit_skips_dry_run_decision(
    field: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    _edit_field_during_offers(p, rig, _FIELD_EDITS[field])
    await _check(p)
    assert (await _decision(p))[:3] == (None, None, None)


async def test_edit_before_quota_skip_does_not_store_decision(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    await _configure(p)
    await add_sent_rows(rig, p.shop, 250, NOW - timedelta(seconds=5))
    _edit_field_during_offers(p, rig, "step=2")
    await _check(p, after=0)
    assert (await _decision(p))[:3] == (None, None, None)


@pytest.mark.parametrize("assignment", ["auto_raise=false", "step=2", "min_price=500"])
@pytest.mark.parametrize("outcome", ["sent", "refused", "unknown"])
async def test_edit_after_send_attempt_keeps_the_outcome_decision(
    outcome: str, assignment: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Отправка была: итог записи нужен, чтобы показать, что произошло, даже после правки."""
    p = await prepare(rig, redis_client, redis_prefix, dry_run=False)
    p.client.price, p.client.competitor_price = 750, 700
    if outcome == "refused":
        p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STOCK_COUNTED, p.sku)
    elif outcome == "unknown":
        p.client.errors["set_price"] = PriceWriteOutcomeUnknownError(p.sku, "timeout")
    await _configure(p)
    _edit_field_during_offers(p, rig, assignment)
    await _check(p)
    assert p.client.methods().count("set_price") == 1
    assert (await _decision(p))[:3] == ("undercut_first", 699, outcome)


async def test_product_deleted_during_check_writes_nothing_and_does_not_fail(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix)
    p.client.price, p.client.competitor_price = 590, 700
    await _configure(p)

    async def delete(method: str, key: str) -> None:
        if method == "get_offers":
            await execute(rig, "DELETE FROM product WHERE id=:i", i=p.product_id)

    p.client.on_call = delete
    await _check(p)
    assert await _rows(p) == []
    assert await fetch(rig, "SELECT id FROM product WHERE id=:i", i=p.product_id) == []
