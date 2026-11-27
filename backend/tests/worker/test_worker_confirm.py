"""Т42–Т46, Т72: подтверждение записи по срокам, перезапуск, аренда в слайсе."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    CITY_ID,
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    anomalies,
    execute,
    fetch,
    journal,
    listed_product,
    not_applied,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    seed_shop,
    shop_row,
    stock_reset,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, add_open_writes, hold_lease, prepare

from kaspisa.db import Database
from kaspisa.kaspi.models import PriceVerification


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


async def _sent(rig: Rig, redis: Redis, prefix: str, **kw: Any) -> Prepared:
    """Запись отправлена в момент NOW; аренда за обработчиком w1."""
    p = await prepare(rig, redis, prefix, **kw)
    result = await p.write(94)
    assert result.outcome.value == "sent"
    await hold_lease(rig, p.shop, "w1", at(100_000))
    return p


async def _verify(p: Prepared, seconds: int) -> None:
    from kaspisa.worker.confirm import verify_open_writes

    p.clock.now = at(seconds)
    await verify_open_writes(p.sctx)


async def _tracked(rig: Rig, p: Prepared) -> bool:
    rows = await fetch(rig, "SELECT tracked FROM product WHERE id=:i", i=p.product_id)
    return bool(rows[0][0])


async def _write_row(rig: Rig, p: Prepared) -> tuple[Any, ...]:
    rows = await fetch(
        rig,
        "SELECT status, next_verify_at, deadline_reported FROM price_write WHERE product_id=:i",
        i=p.product_id,
    )
    return rows[0]


# --- Т42 ---


async def test_confirmation_after_first_check(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    await execute(rig, "UPDATE shop SET write_error_streak=1 WHERE id=:i", i=p.shop.shop_id)
    await _verify(p, 29)
    assert p.client.verified == []
    await _verify(p, 30)
    snapshot, _expected = p.client.sent[0]
    assert p.client.verified == [(snapshot, {CITY_ID: 94})]
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0
    p.clock.now = at(31)
    assert (await p.write(90)).outcome.value == "sent"


# --- Т43 ---


async def test_unconfirmed_write_follows_the_schedule(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    p.client.verify_results = [not_applied() for _ in range(30)]
    for t in range(30, 300, 30):
        await _verify(p, t)
        assert (await _write_row(rig, p))[1] == at(min(t + 30, 300)), t
    assert len(p.client.verified) == 9
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0
    await _verify(p, 300)
    status, next_at, reported = await _write_row(rig, p)
    assert (status, next_at, reported) == ("receipt", at(600), True)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "not_confirmed")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    p.clock.now = at(599)
    assert (await p.write(90)).reason == "hold"
    await _verify(p, 599)
    assert len(p.client.verified) == 10  # на 599 с проверка не назначена
    await _verify(p, 600)  # последняя проверка: запись не применилась
    assert (await _write_row(rig, p))[:2] == ("expired", None)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert len(p.client.verified) == 11
    await _verify(p, 900)
    assert len(p.client.verified) == 11  # больше проверок нет


# --- Т44 ---


async def _until_deadline(p: Prepared) -> None:
    """Проверки по расписанию 30, 60, ..., 300 с: запись не применилась, срок отмечен."""
    p.client.verify_results = [not_applied() for _ in range(10)]
    for t in range(30, 301, 30):
        await _verify(p, t)
    assert len(p.client.verified) == 10
    assert (await _write_row(p.rig, p)) == ("receipt", at(600), True)


async def test_late_application_is_found_by_the_last_check_at_600(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    await _until_deadline(p)
    p.client.verify_results = [anomalies()]
    await _verify(p, 600)
    assert len(p.client.verified) == 11
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "applied", "late_applied")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1  # серию учли на 300 с
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)
    p.clock.now = at(601)
    assert (await p.write(90)).outcome.value == "sent"


async def test_last_check_at_600_without_application_expires_the_write(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    await _until_deadline(p)
    p.client.verify_results = [not_applied()]
    await _verify(p, 600)
    assert len(p.client.verified) == 11
    assert (await _write_row(rig, p))[:2] == ("expired", None)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "not_confirmed")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    await _verify(p, 900)
    assert len(p.client.verified) == 11  # больше проверок нет


async def test_late_application_with_anomalies_halts_the_shop(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    await _until_deadline(p)
    p.client.verify_results = [anomalies("S1")]
    await _verify(p, 600)
    assert len(p.client.verified) == 11
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "applied", "late_applied; store_anomalies:S1")
    ]
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["halt"], row["write_streak"]) == ("store_anomalies", 1)
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)


async def test_row_idle_for_900_seconds_expires_without_a_check(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    await execute(rig, "UPDATE price_write SET next_verify_at=:n", n=at(30))
    await _verify(p, 900)  # реальный простой обработчика
    assert p.client.verified == []
    assert (await _write_row(rig, p))[:3] == ("expired", None, False)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "expired_unchecked")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0


# --- Т45 ---


async def test_store_anomalies_halt_the_shop(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.writer import WriteOutcome

    p = await _sent(rig, redis_client, redis_prefix)
    p.client.verify_results = [anomalies("S2", "S1")]
    await _verify(p, 30)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "applied", "store_anomalies:S1,S2")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["halt"] == "store_anomalies"
    async with p.database.system() as system:
        await execute(
            rig, "UPDATE shop SET claimed_by=NULL, claimed_until=NULL WHERE id=:i", i=p.shop.shop_id
        )
        from kaspisa.worker.constants import (
            LEASE_SECONDS,
            MIN_RECHECK_SECONDS,
            UNBOUNDED_RECHECK_SECONDS,
        )

        claim = await system.claim_shop(
            worker_id="w9",
            now=at(1000),
            lease_seconds=LEASE_SECONDS,
            recheck_seconds=MIN_RECHECK_SECONDS,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
        )
    assert claim is None
    p.clock.now = at(1000)
    result = await p.write(90)
    assert (result.outcome, result.reason) == (WriteOutcome.SKIPPED, "halted")
    assert len(p.client.sent) == 1


# --- Т46 ---


async def test_restart_verifies_with_the_stored_snapshot(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.snapshot_codec import snapshot_to_json

    p = await _sent(rig, redis_client, redis_prefix)
    original = p.client.sent[0][0]
    later = await prepare(
        rig,
        redis_client,
        redis_prefix,
        clock=FakeClock(at(30)),
        shop=p.shop,
        database=rig.new_database(),
        worker_id="w2",
    )
    await hold_lease(rig, p.shop, "w2", at(100_000))
    from kaspisa.worker.confirm import verify_open_writes

    await verify_open_writes(later.sctx)
    ((snapshot, expected),) = later.client.verified
    assert snapshot_to_json(snapshot) == snapshot_to_json(original)
    assert snapshot == original
    assert expected == {CITY_ID: 94}


# --- Т72 ---


async def _many_writes(rig: Rig, redis: Redis, prefix: str, clock: FakeClock) -> Prepared:
    client = FakeKaspiClient(call_seconds=1.0, clock=clock)
    p = await prepare(rig, redis, prefix, products=155, clock=clock, client=client)
    from kaspisa.worker.snapshot_codec import snapshot_to_json

    snapshot_source = await _snapshot(p)
    await add_open_writes(
        rig,
        p.shop,
        p.shop.product_ids[5:],
        snapshot_to_json(snapshot_source),
        sent_at=NOW,
        next_verify_at=at(30),
    )
    await execute(
        rig, "UPDATE shop SET claimed_by=NULL, claimed_until=NULL WHERE id=:i", i=p.shop.shop_id
    )
    return p


async def _snapshot(p: Prepared) -> Any:
    from kaspisa.kaspi.models import ProductSnapshot

    saved = p.clock.now
    state = await p.client.get_product(None, p.shop.merchant_uid, p.sku)  # type: ignore[arg-type]
    p.clock.now = saved
    return ProductSnapshot(state, p.listing(100))


async def test_lease_survives_a_long_verification_run(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.worker.constants import (
        LEASE_SECONDS,
        MAX_VERIFY_PER_SLICE,
        MIN_RECHECK_SECONDS,
        UNBOUNDED_RECHECK_SECONDS,
    )

    clock = FakeClock(at(30))
    p = await _many_writes(rig, redis_client, redis_prefix, clock)
    clock.now = at(30)
    seen: list[tuple[datetime, Any]] = []

    async def spy(method: str, key: str) -> None:
        if method != "verify_price":
            return
        row = await shop_row(rig, p.shop.shop_id)
        seen.append((clock(), row["claimed_until"]))
        async with p.database.system() as system:
            other = await system.claim_shop(
                worker_id="w2",
                now=clock(),
                lease_seconds=LEASE_SECONDS,
                recheck_seconds=MIN_RECHECK_SECONDS,
                unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
            )
        assert other is None

    p.client.on_call = spy
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.methods().count("verify_price") == MAX_VERIFY_PER_SLICE
    assert len(seen) == MAX_VERIFY_PER_SLICE
    for moment, until in seen:
        assert until == moment + timedelta(seconds=LEASE_SECONDS)
    assert "find_product" in p.client.methods()  # товары проверены после подтверждений


async def test_lost_lease_ends_the_slice_without_double_counting(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    clock = FakeClock(at(300))
    p = await _many_writes(rig, redis_client, redis_prefix, clock)
    await execute(rig, "UPDATE price_write SET next_verify_at=:n", n=at(300))
    clock.now = at(300)
    p.client.verify_results = [not_applied() for _ in range(40)]

    async def steal(method: str, key: str) -> None:
        if method == "verify_price" and p.client.methods().count("verify_price") == 3:
            await execute(rig, "UPDATE shop SET claimed_by='other' WHERE id=:i", i=p.shop.shop_id)

    p.client.on_call = steal
    assert await run_one_slice(p.ctx, p.database)
    assert p.client.methods().count("verify_price") == 3
    assert "find_product" not in p.client.methods()
    reported = await fetch(rig, "SELECT count(*) FROM price_write WHERE deadline_reported")
    assert reported == [(3,)]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 3


async def _claim_by_second_instance(p: Prepared, at_time: datetime) -> Any:
    async with p.database.system() as system:
        return await system.claim_shop(
            worker_id="w2",
            now=at_time,
            lease_seconds=240,
            recheck_seconds=0,
            unbounded_recheck_seconds=0,
        )


async def test_lease_survives_long_limiter_waits_before_each_product_request(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т72: ожидание перед каждым из 4 запросов товара; аренда не истекает, второй не берёт."""
    clock = FakeClock(NOW)
    client = FakeKaspiClient(call_seconds=100.0, clock=clock)
    p = await prepare(rig, redis_client, redis_prefix, clock=clock, client=client)
    seen: list[tuple[str, datetime, Any]] = []

    async def spy(method: str, key: str) -> None:
        row = await shop_row(rig, p.shop.shop_id)
        seen.append((method, clock(), row["claimed_until"]))
        assert await _claim_by_second_instance(p, clock()) is None

    client.on_call = spy
    assert await run_one_slice(p.ctx, p.database)
    methods = [method for method, _, _ in seen]
    assert methods == ["find_product", "get_offers", "get_product", "find_product", "set_price"]
    assert clock() - NOW >= timedelta(seconds=500)  # без продления аренда истекла бы
    for method, moment, until in seen:
        assert until is not None and until >= moment + timedelta(seconds=100), method
    assert len(p.client.sent) == 1


@pytest.mark.parametrize(
    ("lost_at", "occurrence"), [("get_offers", 1), ("get_product", 1), ("find_product", 2)]
)
async def test_lost_lease_between_chain_requests_stops_the_slice_without_sending(
    lost_at: str, occurrence: int, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т72: аренда потеряна между запросами цепочки записи: отправки нет, следов записи нет."""
    clock = FakeClock(NOW)
    client = FakeKaspiClient(call_seconds=1.0, clock=clock)
    p = await prepare(rig, redis_client, redis_prefix, clock=clock, client=client)

    async def steal(method: str, key: str) -> None:
        if method == lost_at and client.methods().count(method) == occurrence:
            await execute(rig, "UPDATE shop SET claimed_by='other' WHERE id=:i", i=p.shop.shop_id)

    client.on_call = steal
    assert await run_one_slice(p.ctx, p.database)
    assert "set_price" not in client.methods()
    assert client.sent == []
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    assert await fetch(rig, "SELECT 1 FROM price_change") == []
    assert (await shop_row(rig, p.shop.shop_id))["claimed_by"] == "other"


# --- граница 600 и 900 с (срок 300 с отмечается по времени) ---


async def test_row_at_600_without_deadline_mark_gets_the_last_check(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    await execute(rig, "UPDATE price_write SET next_verify_at=:n", n=at(30))
    p.client.verify_results = [not_applied()]
    await _verify(p, 600)
    assert len(p.client.verified) == 1
    assert (await _write_row(rig, p))[:3] == ("expired", None, True)
    assert [r[4] for r in await journal(rig, p.shop.shop_id)] == ["not_confirmed"]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1


async def test_row_at_899_without_deadline_mark_is_still_checked(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent(rig, redis_client, redis_prefix)
    p.client.verify_results = [not_applied()]
    await execute(rig, "UPDATE price_write SET next_verify_at=:n", n=at(30))
    await _verify(p, 899)
    assert len(p.client.verified) == 1
    assert (await _write_row(rig, p))[:3] == ("expired", None, True)


async def test_failed_check_at_300_marks_the_deadline_and_last_check_still_runs(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т44 (г): сбой проверки ровно в 300 с отмечает срок; проверка в 600 с выполняется."""
    from kaspisa.kaspi import KaspiTransportError

    p = await _sent(rig, redis_client, redis_prefix)
    p.client.verify_results = [not_applied() for _ in range(9)]
    for t in range(30, 300, 30):
        await _verify(p, t)
    p.client.verify_results = [KaspiTransportError("verify_price", "timeout")]
    await _verify(p, 300)
    assert (await _write_row(rig, p)) == ("receipt", at(600), True)
    assert [r[4] for r in await journal(rig, p.shop.shop_id)] == ["not_confirmed"]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    p.client.verify_results = [anomalies("S1")]
    await _verify(p, 600)
    assert [r[3:] for r in await journal(rig, p.shop.shop_id)] == [
        ("applied", "late_applied; store_anomalies:S1")
    ]
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["halt"], row["write_streak"]) == ("store_anomalies", 1)


async def test_late_first_successful_check_counts_the_missed_deadline_once(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т44 (д): первая успешная проверка запоздала (450 с), отметки срока не было."""
    p = await _sent(rig, redis_client, redis_prefix)
    await execute(rig, "UPDATE price_write SET next_verify_at=:n", n=at(450))
    await _verify(p, 450)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "applied", "late_applied")
    ]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)


async def test_unknown_write_is_not_counted_twice_in_the_series(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т43: исход `unknown` учтён при отправке; срок 300 с серию не удваивает."""
    from kaspisa.kaspi import PriceWriteOutcomeUnknownError

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = PriceWriteOutcomeUnknownError(p.sku, "timeout")
    await p.write(94)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    p.client.verify_results = [not_applied() for _ in range(12)]
    for t in range(30, 601, 30):
        await _verify(p, t)
    assert (await _write_row(rig, p))[:3] == ("expired", None, True)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert [r[4] for r in await journal(rig, p.shop.shop_id)] == ["not_confirmed"]


async def test_failed_last_check_at_600_expires_and_before_it_reschedules(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Сбой самой проверки: на 600 с запись истекает, на 599 с остаётся до 600 с."""
    from kaspisa.kaspi import KaspiTransportError

    p = await _sent(rig, redis_client, redis_prefix)
    await execute(
        rig, "UPDATE price_write SET deadline_reported=true, next_verify_at=:n", n=at(300)
    )
    p.client.verify_results = [KaspiTransportError("verify_price", "timeout")] * 2
    await _verify(p, 599)
    status, next_verify_at, _ = await _write_row(rig, p)
    assert (status, next_verify_at) == ("receipt", at(600))
    await _verify(p, 600)
    assert (await _write_row(rig, p))[:2] == ("expired", None)


# --- граница 300 с и серия ошибок ---


async def test_applied_at_299_is_in_time_and_at_300_is_late_with_one_series_error(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т44: `applied` до 300 с обнуляет серию; с 300 с - `late_applied` и одна ошибка серии."""
    for seconds, error, streak in ((299, None, 0), (300, "late_applied", 2)):
        p = await _sent(rig, redis_client, f"{redis_prefix}:{seconds}")
        await execute(rig, "UPDATE shop SET write_error_streak=1 WHERE id=:i", i=p.shop.shop_id)
        await _verify(p, seconds)
        assert [r[3:] for r in await journal(rig, p.shop.shop_id)] == [("applied", error)]
        assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == streak
        await execute(rig, "DELETE FROM price_write")
        await execute(rig, "DELETE FROM price_change")


async def test_unknown_write_applied_at_300_does_not_add_a_series_error(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Исход `unknown` учтён при отправке: позднее применение серию не удваивает."""
    from kaspisa.kaspi import PriceWriteOutcomeUnknownError

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = PriceWriteOutcomeUnknownError(p.sku, "timeout")
    await p.write(94)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    await _verify(p, 300)
    assert [r[3:] for r in await journal(rig, p.shop.shop_id)] == [("applied", "late_applied")]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1


# --- Н9: признак «ошибка учтена при отправке» (Т43, Т44 д) ---


async def _reconnect(rig: Rig, p: Prepared) -> None:
    """После `SessionExpiredError` магазин `reauth_required`; для проверки возвращаем доступ."""
    await execute(
        rig,
        "UPDATE shop SET connection_state='connected', connection_error=NULL WHERE id=:i",
        i=p.shop.shop_id,
    )
    await p.sctx.access.refresh()


async def _error_counted(rig: Rig, p: Prepared) -> bool:
    rows = await fetch(
        rig, "SELECT error_counted FROM price_write WHERE product_id=:i", i=p.product_id
    )
    return bool(rows[0][0])


def _counted_errors() -> list[Any]:
    import asyncio

    from kaspisa.kaspi import (
        PriceWriteOutcomeUnknownError,
        ResponseFormatError,
        UnexpectedStatusError,
    )

    return [
        asyncio.CancelledError(),
        PriceWriteOutcomeUnknownError("sku", "timeout"),
        ResponseFormatError("set_price"),
        TimeoutError(),
        UnexpectedStatusError("set_price", 500),
    ]


@pytest.mark.parametrize("index", range(5))
async def test_write_errors_counted_at_send_are_not_counted_again_at_300(
    index: int, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т43: отмена, неизвестный исход, формат, тайм-аут, статус: серия 2, а не 4."""
    import asyncio

    p = await prepare(rig, redis_client, redis_prefix)
    await execute(rig, "UPDATE shop SET write_error_streak=1 WHERE id=:i", i=p.shop.shop_id)
    error = _counted_errors()[index]
    p.client.errors["set_price"] = error
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await p.write(94)
    else:
        await p.write(94)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 2
    assert await _error_counted(rig, p) is True
    p.client.verify_results = [not_applied() for _ in range(12)]
    for t in range(30, 601, 30):
        await _verify(p, t)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 2
    assert await _error_counted(rig, p) is True


async def test_session_expired_write_is_counted_once_at_300(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т43: после `SessionExpiredError` серия не растёт при отправке, на сроке - +1 один раз."""
    from kaspisa.kaspi import SessionExpiredError

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = SessionExpiredError("set_price")
    await p.write(94)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    await _reconnect(rig, p)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0
    assert await _error_counted(rig, p) is False
    p.client.verify_results = [not_applied() for _ in range(12)]
    for t in range(30, 601, 30):
        await _verify(p, t)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert await _error_counted(rig, p) is True


async def test_session_expired_write_applied_late_is_counted_once(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т44 (д): запись после `SessionExpiredError`, позднее применение - +1 один раз."""
    from kaspisa.kaspi import SessionExpiredError

    p = await prepare(rig, redis_client, redis_prefix)
    p.client.errors["set_price"] = SessionExpiredError("set_price")
    await p.write(94)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    await _reconnect(rig, p)
    await execute(rig, "UPDATE price_write SET next_verify_at=:n", n=at(300))
    await _verify(p, 300)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert await _error_counted(rig, p) is True


# --- Т89 (ADR 0034): подтверждение при выключенном боте не входит в кабинет ---


@pytest.mark.parametrize(("mode", "logs_in"), [("off", False), ("dry_run", True), ("live", True)])
async def test_expired_session_at_verification_logs_in_only_when_bot_runs(
    mode: str, logs_in: bool, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т89: истёкшая сессия при проверке записи: автовход в `dry_run`/`live`, в `off` нет.

    Во всех режимах слайс заканчивается (`ShopUnavailable`). При `off` запись остаётся
    в ожидании с прежним сроком проверки, магазин - `reauth_required`, серия записи не
    растёт, применённой запись не считается.
    """
    from kaspisa.kaspi import SessionExpiredError
    from kaspisa.worker.errors import ShopUnavailable

    p = await _sent(rig, redis_client, redis_prefix)
    open_status = (await _write_row(rig, p))[0]
    await execute(rig, "UPDATE shop SET bot_mode=:m WHERE id=:i", m=mode, i=p.shop.shop_id)
    await p.sctx.access.refresh()
    p.client.verify_results = [SessionExpiredError("verify_price")]
    with pytest.raises(ShopUnavailable):
        await _verify(p, 300)
    assert ("login" in p.client.methods()) is logs_in
    if logs_in:
        return
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["state"], row["write_streak"]) == ("reauth_required", 0)
    status, next_verify_at, reported = await _write_row(rig, p)
    assert (status, next_verify_at, reported) == (open_status, at(30), False)
    assert [r[3] for r in await journal(rig, p.shop.shop_id)] == ["unconfirmed"]


@pytest.mark.parametrize("mode", ["dry_run", "live"])
async def test_expired_session_at_verification_of_a_halted_shop_does_not_log_in(
    mode: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т91 (ADR 0034, Н1): остановленный магазин при подтверждении не входит в кабинет.

    Правило то же, что в `ShopAccess.open`: автовход только при `bot_mode <> 'off'` и без
    остановки. Слайс заканчивается, запись остаётся ждать с прежним сроком.
    """
    from kaspisa.kaspi import SessionExpiredError
    from kaspisa.worker.errors import ShopUnavailable

    p = await _sent(rig, redis_client, redis_prefix)
    open_status = (await _write_row(rig, p))[0]
    await execute(
        rig,
        "UPDATE shop SET bot_mode=:m, halt_reason='write_errors', halted_at=now() WHERE id=:i",
        m=mode,
        i=p.shop.shop_id,
    )
    await p.sctx.access.refresh()
    p.client.verify_results = [SessionExpiredError("verify_price")]
    with pytest.raises(ShopUnavailable):
        await _verify(p, 300)
    assert "login" not in p.client.methods()
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["state"], row["halt"]) == ("reauth_required", "write_errors")
    status, next_verify_at, reported = await _write_row(rig, p)
    assert (status, next_verify_at, reported) == (open_status, at(30), False)
    assert [r[3] for r in await journal(rig, p.shop.shop_id)] == ["unconfirmed"]


async def test_bot_switched_off_mid_slice_ends_the_slice_without_a_product_skip(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т90: режим сменили на `off` посреди слайса, сессия истекла на чтении товара.

    Входа нет, слайс заканчивается, причина пропуска у товара не записывается.
    """
    from kaspisa.kaspi import SessionExpiredError
    from kaspisa.worker.cycle import check_product
    from kaspisa.worker.errors import ShopUnavailable

    p = await prepare(rig, redis_client, redis_prefix)
    await hold_lease(rig, p.shop, "w1", at(100_000))
    await execute(rig, "UPDATE shop SET bot_mode='off' WHERE id=:i", i=p.shop.shop_id)
    await p.sctx.access.refresh()
    p.client.errors["find_product"] = SessionExpiredError("find_product")
    async with p.database.tenant(p.shop.tenant_id) as scope:
        product = await scope.products.get(p.product_id)
    assert product is not None
    with pytest.raises(ShopUnavailable):
        await check_product(p.sctx, product)
    assert "login" not in p.client.methods()
    assert (await shop_row(rig, p.shop.shop_id))["state"] == "reauth_required"


# --- этап 23 (ADR 0037): вторая проверка остатков у записи с остатком ---


async def _sent_stock(rig: Rig, redis: Redis, prefix: str, stock: int | None) -> Prepared:
    """Запись, у которой точка `S1` имеет остаток `stock` в чтении и строке."""
    client = FakeKaspiClient()
    client.stock_count = stock
    return await _sent(rig, redis, prefix, client=client)


def _verdict(
    *, applied: bool, stores: tuple[str, ...] = (), mismatch: bool = False
) -> PriceVerification:
    mismatches: dict[str, tuple[int, int | None]] = {CITY_ID: (94, 95)} if mismatch else {}
    return PriceVerification(
        applied=applied, listing=None, mismatches=mismatches, store_anomalies=stores
    )


async def _checks_until_300(p: Prepared) -> None:
    """Проверки на 30, 60, ..., 270 с: запись не применилась."""
    p.client.verify_results = [not_applied() for _ in range(9)]
    for t in range(30, 300, 30):
        await _verify(p, t)
    assert len(p.client.verified) == 9


@pytest.mark.parametrize("stock", [5, 1, 0, None])
async def test_counted_write_schedules_a_stock_check(
    stock: int | None, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent_stock(rig, redis_client, redis_prefix, stock)
    await _verify(p, 30)
    assert p.client.verified_settled == [False]
    status, next_at, _ = await _write_row(rig, p)
    assert (status, next_at) == ("confirmed", at(300) if stock in (5, 1) else None)
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 0
    p.clock.now = at(31)
    assert (await p.write(90)).outcome.value == "sent"


async def test_stock_check_at_300_without_anomalies(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    await _verify(p, 299)
    assert p.client.verified_settled == [False]
    await _verify(p, 300)
    assert p.client.verified_settled == [False, True]
    assert (await _write_row(rig, p)) == ("confirmed", None, False)
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["write_streak"], row["halt"]) == (0, None)
    await _verify(p, 330)
    assert p.client.verified_settled == [False, True]


@pytest.mark.parametrize("case", ["a", "b", "c", "d", "e"])
async def test_stock_check_anomalies_halt_or_untrack(
    case: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т20: прочие аномалии останавливают магазин, сброс остатка выключает товар."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    verdicts = {
        "a": _verdict(applied=True, stores=("S1",)),
        "b": _verdict(applied=False, stores=("S1",), mismatch=True),
        "c": stock_reset(),
        "d": stock_reset(applied=False, mismatches={CITY_ID: (94, 95)}),
        "e": stock_reset(extra=("S2",)),
    }
    p.client.verify_results = [verdicts[case]]
    await _verify(p, 300)
    halt, note, tracked = {
        "a": ("store_anomalies", "store_anomalies:S1", True),
        "b": ("store_anomalies", "store_anomalies:S1", True),
        "c": (None, "stock_reset:S1", False),
        "d": (None, "stock_reset:S1", False),
        "e": ("store_anomalies", "store_anomalies:S2; stock_reset:S1", False),
    }[case]
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["halt"], row["write_streak"]) == (halt, 0)
    assert await _tracked(rig, p) is tracked
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", note)]
    assert (await _write_row(rig, p)) == ("confirmed", None, False)


async def test_stock_check_after_idle_still_runs(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    await _verify(p, 950)
    assert p.client.verified_settled == [False, True]
    assert (await _write_row(rig, p)) == ("confirmed", None, False)
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]


async def test_failed_stock_check_retries_until_600(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    from kaspisa.kaspi import KaspiTransportError

    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    p.client.verify_results = [KaspiTransportError("verify_price", "timeout") for _ in range(3)]
    for t, expected in ((300, at(330)), (330, at(360)), (600, None)):
        await _verify(p, t)
        status, next_at, _ = await _write_row(rig, p)
        assert (status, next_at) == ("confirmed", expected), t
        assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]
        assert (await shop_row(rig, p.shop.shop_id))["halt"] is None


@pytest.mark.parametrize("stock", [5, None])
async def test_late_counted_confirmation_is_already_strict(
    stock: int | None, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent_stock(rig, redis_client, redis_prefix, stock)
    await _checks_until_300(p)
    p.client.verify_results = [anomalies()]
    await _verify(p, 300)
    assert p.client.verified_settled[-1] is (stock == 5)
    assert [r[3:] for r in await journal(rig, p.shop.shop_id)] == [("applied", "late_applied")]
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)


@pytest.mark.parametrize("case", ["a", "b", "c"])
async def test_first_check_anomalies_skip_the_stock_check(
    case: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т24: аномалии первой проверки отменяют вторую; сброс остатка выключает товар."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    p.client.verify_results = [
        {"a": anomalies("S1"), "b": stock_reset(), "c": stock_reset(extra=("S2",))}[case]
    ]
    await _verify(p, 30)
    halt, note, tracked = {
        "a": ("store_anomalies", "store_anomalies:S1", True),
        "b": (None, "stock_reset:S1", False),
        "c": ("store_anomalies", "store_anomalies:S2; stock_reset:S1", False),
    }[case]
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["halt"], row["write_streak"]) == (halt, 0)
    assert await _tracked(rig, p) is tracked
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", note)]
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)


@pytest.mark.parametrize("stock", [5, None])
async def test_unapplied_counted_write_halts_on_store_anomalies(
    stock: int | None, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent_stock(rig, redis_client, redis_prefix, stock)
    await _checks_until_300(p)
    p.client.verify_results = [_verdict(applied=False, stores=("S1",), mismatch=True)]
    await _verify(p, 300)
    counted = stock == 5
    assert p.client.verified_settled[-1] is counted
    assert (await shop_row(rig, p.shop.shop_id))["halt"] == ("store_anomalies" if counted else None)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "not_confirmed")
    ]
    assert (await _write_row(rig, p)) == ("receipt", at(600), True)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    assert await _tracked(rig, p) is True


async def test_unapplied_counted_write_with_reset_untracks_the_product(
    rig: Rig, redis_client: Redis, redis_prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Т25 (в): цена не подтверждена, остаток сброшен - товар выключен, магазин не остановлен."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _checks_until_300(p)
    caplog.set_level(logging.WARNING)
    reset = stock_reset(applied=False, mismatches={CITY_ID: (94, 95)})
    p.client.verify_results = [reset]
    await _verify(p, 300)
    assert p.client.verified_settled[-1] is True
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
    assert await _tracked(rig, p) is False
    note = "not_confirmed; stock_reset:S1"
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", note)]
    assert (await _write_row(rig, p)) == ("receipt", at(600), True)
    assert (await shop_row(rig, p.shop.shop_id))["write_streak"] == 1
    p.client.verify_results = [stock_reset(applied=False, mismatches={CITY_ID: (94, 95)})]
    await _verify(p, 600)
    status, next_at, _ = await _write_row(rig, p)
    assert (status, next_at) == ("expired", None)
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "unconfirmed", note)]
    assert await _tracked(rig, p) is False
    assert sum("отслеживание выключено" in m for m in _warnings(caplog)) == 1


async def test_unapplied_write_before_300_ignores_anomalies(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    p.client.verify_results = [_verdict(applied=False, stores=("S1",), mismatch=True)]
    await _verify(p, 30)
    assert p.client.verified_settled == [False]
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
    assert (await _write_row(rig, p))[:2] == ("receipt", at(60))
    assert [r[3] for r in await journal(rig, p.shop.shop_id)] == ["unconfirmed"]


# --- этап 23 (ADR 0037, раздел 12): сброс остатка, замеченный до поздней проверки ---

_PRICE_WRITE_ROW = (
    "SELECT status, sent_at, next_verify_at, price_change_id, job_id FROM price_write "
    "WHERE product_id=:i"
)


async def _full_write_row(rig: Rig, p: Prepared) -> tuple[Any, ...]:
    return (await fetch(rig, _PRICE_WRITE_ROW, i=p.product_id))[0]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize("case", ["confirmed", "confirmed_read_reset", "receipt"])
async def test_reset_seen_before_next_write_untracks_the_product(
    case: str,
    rig: Rig,
    redis_client: Redis,
    redis_prefix: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Т28: прежняя запись с остатком ещё ждёт проверки, а в списке остаток уже сброшен."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    if case == "receipt":
        attempt = 10
    else:
        await _verify(p, 30)
        assert (await _write_row(rig, p))[:2] == ("confirmed", at(300))
        attempt = 61
    p.clock.now = at(attempt)
    p.client.listed_stock_count = None
    p.client.stock_count = None if case == "confirmed_read_reset" else 5
    row_before = await _full_write_row(rig, p)
    journal_before = await journal(rig, p.shop.shop_id)
    p.client.calls.clear()
    caplog.set_level(logging.WARNING)

    result = await p.write(90)

    assert (result.outcome.value, result.reason) == ("skipped", "not_tracked")
    assert p.client.methods() == ["get_product", "find_product"]
    assert len(p.client.sent) == 1
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
    assert await _tracked(rig, p) is False
    assert await _full_write_row(rig, p) == row_before
    assert await journal(rig, p.shop.shop_id) == journal_before
    disabled = [m for m in _warnings(caplog) if "отслеживание выключено" in m]
    assert len(disabled) == 1
    assert str(p.product_id) in disabled[0] and "S1" in disabled[0]
    if case == "confirmed":  # поздняя проверка прежней записи: примечание есть, повтора нет
        p.client.verify_results = [stock_reset()]
        await _verify(p, 300)
        assert p.client.verified_settled[-1] is True
        assert await _tracked(rig, p) is False
        assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
        assert await journal(rig, p.shop.shop_id) == [
            (100, 94, "undercut_first", "applied", "stock_reset:S1")
        ]
        assert (await _write_row(rig, p))[1] is None
        assert sum("отслеживание выключено" in m for m in _warnings(caplog)) == 1


_RESTORED_ROW = (
    "SELECT status, sent_at, price_change_id, job_id, next_verify_at, deadline_reported, "
    "error_counted, expected_prices, snapshot FROM price_write WHERE product_id=:i"
)


@pytest.mark.parametrize("case", ["refused", "throttled", "no_stock"])
async def test_discarded_attempt_restores_pending_stock_check(
    case: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т33: компенсация непринятой записи возвращает строку с ожидающей поздней проверкой."""
    from kaspisa.kaspi import RateLimitedError, WriteRefusal, WriteRefusedError
    from kaspisa.worker.errors import ShopThrottled

    p = await _sent_stock(rig, redis_client, redis_prefix, None if case == "no_stock" else 5)
    await _verify(p, 30)
    first = (await _full_write_row(rig, p))[3]
    p.clock.now = at(61)
    p.client.stock_count = 3
    p.client.listed_stock_count = 3
    if case == "throttled":
        p.client.errors["set_price"] = RateLimitedError("set_price", 5)
    else:
        p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STALE_LISTING, p.sku)
    rows_before = len(await journal(rig, p.shop.shop_id))
    if case == "throttled":
        with pytest.raises(ShopThrottled):
            await p.write(90)
    else:
        result = await p.write(90)
        assert (result.outcome.value, result.reason) == ("refused", "stale_listing")
    if case == "no_stock":
        assert await fetch(rig, _RESTORED_ROW, i=p.product_id) == []
        return
    row = (await fetch(rig, _RESTORED_ROW, i=p.product_id))[0]
    status, sent_at, change_id, job_id, next_at, reported, counted, expected, snap = row
    assert (status, sent_at, change_id, job_id) == ("confirmed", at(0), first, "job-1")
    assert (next_at, reported, counted) == (at(300), False, False)
    assert expected == {CITY_ID: 94}
    point = snap["state"]["cities"][0]["points"][0]
    assert (point["id"], point["stock"]) == ("S1", 5)
    added = 1 if case == "refused" else 0  # отказ клиента добавляет строку `failed`
    assert len(await journal(rig, p.shop.shop_id)) == rows_before + added
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
    p.client.errors.clear()
    p.client.verify_results = [anomalies()]
    await _verify(p, 300)
    snapshot, _ = p.client.verified[-1]
    assert p.client.verified_settled[-1] is True
    assert snapshot.state.cities[0].points[0].stock == 5
    assert (await _write_row(rig, p))[1] is None


async def test_next_write_without_reset_replaces_the_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т29: остаток в списке на месте - новая запись идёт, частота не ограничена (Н18)."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    p.clock.now = at(61)
    p.client.stock_count = 3
    p.client.listed_stock_count = 3
    result = await p.write(90)
    assert result.outcome.value == "sent"
    assert len(p.client.sent) == 2
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
    status, sent_at, next_at, change_id, _ = await _full_write_row(rig, p)
    assert (status, sent_at, next_at) == ("receipt", at(61), at(61) + timedelta(seconds=30))
    last = await fetch(rig, "SELECT max(id) FROM price_change WHERE shop_id=:i", i=p.shop.shop_id)
    assert change_id == last[0][0]


async def test_closed_stock_check_does_not_block_next_write(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т30: поздняя проверка закрыта - прежний снимок больше не даёт остановки."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    await _verify(p, 300)
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)
    p.clock.now = at(301)
    p.client.listed_stock_count = None
    result = await p.write(90)
    assert result.outcome.value == "sent"
    assert len(p.client.sent) == 2
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None


async def test_write_without_stock_is_not_checked_for_reset(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т31: прежний снимок без остатка не даёт остановки; идёт обычный запрет `hold`."""
    p = await _sent_stock(rig, redis_client, redis_prefix, None)
    p.clock.now = at(10)
    result = await p.write(90)
    assert (result.outcome.value, result.reason) == ("skipped", "hold")
    assert len(p.client.sent) == 1
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None


async def test_stock_check_without_listing_logs_a_warning(
    rig: Rig, redis_client: Redis, redis_prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Т32: поздняя проверка не нашла строку товара - запись закрыта, в логе предупреждение."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    p.client.verify_results = [_verdict(applied=False)]
    caplog.set_level(logging.WARNING)
    await _verify(p, 300)
    assert (await _write_row(rig, p))[:2] == ("confirmed", None)
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "applied", None)]
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None
    assert any(
        str(p.product_id) in m and "строка товара не найдена" in m for m in _warnings(caplog)
    )


# --- этап 23: дополнения по итогам мутаций ---


async def test_first_check_reset_of_two_points_lists_both_in_the_note(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Сброс остатка у двух точек: в примечании обе, через запятую, товар выключен."""
    client = FakeKaspiClient()
    client.stock_count = 5
    client.extra_stocks = {"S2": 3}
    p = await _sent(rig, redis_client, redis_prefix, client=client)
    listing = listed_product(p.sku, p.shop.merchant_uid, 94, None, extra_stores={"S2": None})
    p.client.verify_results = [
        PriceVerification(
            applied=True, listing=listing, mismatches={}, store_anomalies=("S1", "S2")
        )
    ]
    await _verify(p, 30)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "applied", "stock_reset:S1,S2")
    ]
    assert await _tracked(rig, p) is False
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None


async def test_anomaly_without_reset_is_not_a_reset_even_if_the_listing_looks_like_one(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Сбросом считается только точка из списка аномалий ответа: S1 не названа - товар остаётся."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    p.client.verify_results = [
        PriceVerification(
            applied=True,
            listing=listed_product(p.sku, p.shop.merchant_uid, 94, None),
            mismatches={},
            store_anomalies=("S2",),
        )
    ]
    await _verify(p, 30)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "applied", "store_anomalies:S2")
    ]
    assert await _tracked(rig, p) is True
    assert (await shop_row(rig, p.shop.shop_id))["halt"] == "store_anomalies"


async def test_reset_found_by_the_last_check_after_a_failed_300_is_noted(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Срок 300 уже отмечен без ответа; на 600 найден сброс - примечание и выключение."""
    from kaspisa.kaspi import KaspiTransportError

    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _checks_until_300(p)
    p.client.verify_results = [KaspiTransportError("verify_price", "timeout")]
    await _verify(p, 300)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "not_confirmed")
    ]
    assert await _tracked(rig, p) is True
    p.client.verify_results = [stock_reset(applied=False, mismatches={CITY_ID: (94, 95)})]
    await _verify(p, 600)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "unconfirmed", "not_confirmed; stock_reset:S1")
    ]
    assert await _tracked(rig, p) is False
    assert (await shop_row(rig, p.shop.shop_id))["halt"] is None


async def test_restored_check_keeps_the_flags_of_the_replaced_row(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Компенсация возвращает отметки `deadline_reported` и `error_counted` как были."""
    from kaspisa.kaspi import WriteRefusal, WriteRefusedError

    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    await execute(
        rig,
        "UPDATE price_write SET deadline_reported=true, error_counted=true WHERE product_id=:i",
        i=p.product_id,
    )
    p.clock.now = at(61)
    p.client.stock_count = 3
    p.client.listed_stock_count = 3
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STALE_LISTING, p.sku)
    assert (await p.write(90)).outcome.value == "refused"
    row = (await fetch(rig, _RESTORED_ROW, i=p.product_id))[0]
    assert (row[0], row[4], row[5], row[6]) == ("confirmed", at(300), True, True)


async def test_stale_unverified_row_is_not_restored_after_a_discarded_write(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Прежняя запись в статусе `receipt` (запрет истёк) не возвращается как подтверждённая."""
    from kaspisa.kaspi import WriteRefusal, WriteRefusedError

    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    p.clock.now = at(650)
    p.client.listed_stock_count = 5
    p.client.errors["set_price"] = WriteRefusedError(WriteRefusal.STALE_LISTING, p.sku)
    assert (await p.write(90)).outcome.value == "refused"
    assert await fetch(rig, _RESTORED_ROW, i=p.product_id) == []


@pytest.mark.parametrize("case", ["busy", "unavailable", "session_state", "kind_busy", "kind_down"])
async def test_every_discard_path_restores_pending_stock_check(
    case: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Любая компенсация (ограничитель, фаза сессии) возвращает ожидающую позднюю проверку."""
    from kaspisa.kaspi import PriceWriteOutcomeUnknownError, SessionStateError
    from kaspisa.worker.errors import ShopUnavailable
    from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError

    errors: dict[str, tuple[BaseException, type[BaseException]]] = {
        "busy": (LimiterBusyError(5), LimiterBusyError),
        "unavailable": (LimiterUnavailableError("x"), LimiterUnavailableError),
        "session_state": (SessionStateError("phase"), ShopUnavailable),
        "kind_busy": (PriceWriteOutcomeUnknownError("sku", "LimiterBusyError"), LimiterBusyError),
        "kind_down": (
            PriceWriteOutcomeUnknownError("sku", "LimiterUnavailableError"),
            LimiterUnavailableError,
        ),
    }
    raised, expected = errors[case]
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    p.clock.now = at(61)
    p.client.stock_count = 3
    p.client.listed_stock_count = 3
    p.client.errors["set_price"] = raised
    with pytest.raises(expected):
        await p.write(90)
    row = (await fetch(rig, _RESTORED_ROW, i=p.product_id))[0]
    assert (row[0], row[1], row[3], row[4]) == ("confirmed", at(0), "job-1", at(300))
    assert len(await journal(rig, p.shop.shop_id)) == 1


# --- этап 24 (ADR 0038): отметка сброса для уведомления в Telegram ---


async def _reset_columns(rig: Rig, p: Prepared) -> tuple[Any, ...]:
    rows = await fetch(
        rig,
        "SELECT stock_reset_at, stock_reset_points FROM product WHERE id=:i",
        i=p.product_id,
    )
    return rows[0]


@pytest.mark.parametrize("path", ["p1", "p2", "p3", "p4"])
async def test_untrack_records_reset_time_and_points(
    path: str, rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т7: все четыре пути выключения пишут время и точки сброса."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    if path == "p1":
        p.clock.now = at(10)
        p.client.listed_stock_count = None
        await p.write(90)
        moment = at(10)
    elif path == "p2":
        await _verify(p, 30)
        p.client.verify_results = [stock_reset()]
        await _verify(p, 300)
        moment = at(300)
    elif path == "p3":
        p.client.verify_results = [stock_reset()]
        await _verify(p, 30)
        moment = at(30)
    else:
        await _checks_until_300(p)
        p.client.verify_results = [stock_reset(applied=False, mismatches={CITY_ID: (94, 95)})]
        await _verify(p, 300)
        moment = at(300)
    assert await _tracked(rig, p) is False
    assert await _reset_columns(rig, p) == (moment, "S1")


async def test_repeated_reset_keeps_first_time(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т8: повторно обнаруженный сброс не сдвигает время и не меняет точки."""
    p = await _sent_stock(rig, redis_client, redis_prefix, 5)
    await _verify(p, 30)
    p.clock.now = at(61)
    p.client.listed_stock_count = None
    await p.write(90)
    assert await _reset_columns(rig, p) == (at(61), "S1")
    p.client.verify_results = [stock_reset(extra=("S2",))]
    await _verify(p, 300)
    assert await _reset_columns(rig, p) == (at(61), "S1")


async def test_two_points_are_stored_sorted(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    """Т9: две точки сброса хранятся через запятую, по возрастанию."""
    client = FakeKaspiClient()
    client.stock_count = 5
    client.extra_stocks = {"S2": 3}
    p = await _sent(rig, redis_client, redis_prefix, client=client)
    listing = listed_product(p.sku, p.shop.merchant_uid, 94, None, extra_stores={"S2": None})
    p.client.verify_results = [
        PriceVerification(
            applied=True, listing=listing, mismatches={}, store_anomalies=("S2", "S1")
        )
    ]
    await _verify(p, 30)
    assert await _reset_columns(rig, p) == (at(30), "S1,S2")
