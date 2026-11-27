"""Т7–Т12, Т20: методы репозиториев, нужные обработчику."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from db_worker_seed import (
    NOW,
    ago,
    insert_change,
    insert_product,
    insert_shop,
    insert_tenant,
    insert_write,
)
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from kaspisa.db import (
    Database,
    NotFoundError,
    PriceChangeResult,
    TenantId,
    create_database,
)
from kaspisa.security import Secret, SecretCipher
from kaspisa.worker.constants import UNBOUNDED_RECHECK_SECONDS


def _t(tenant: uuid.UUID) -> TenantId:
    return TenantId(tenant)


# --- Т7: next_due ---


async def test_next_due_filters_and_order(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    never = await insert_product(connection, tenant, shop, last_checked_at=None)
    old = await insert_product(connection, tenant, shop, last_checked_at=ago(300))
    edge = await insert_product(connection, tenant, shop, last_checked_at=ago(60))
    await insert_product(connection, tenant, shop, last_checked_at=ago(59))
    await insert_product(connection, tenant, shop, tracked=False)
    no_min = await insert_product(connection, tenant, shop, min_price=None, last_checked_at=None)
    no_max = await insert_product(
        connection, tenant, shop, max_price=None, last_checked_at=ago(600)
    )
    await insert_product(connection, tenant, shop, min_price=None, last_checked_at=ago(599))
    await insert_product(connection, tenant, shop, retry_at=NOW + timedelta(seconds=1))
    await insert_product(
        connection, tenant, shop, min_price=None, retry_at=NOW + timedelta(seconds=1)
    )
    await insert_product(connection, tenant, shop, min_price=None, tracked=False)
    past = await insert_product(connection, tenant, shop, retry_at=NOW, last_checked_at=ago(100))

    async def drain(*, bounded: bool) -> list[int]:
        got: list[int] = []
        async with database.tenant(_t(tenant)) as scope:
            for _ in range(8):
                product = await scope.products.next_due(
                    shop,
                    now=NOW,
                    recheck_seconds=60,
                    unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
                    bounded=bounded,
                )
                if product is None:
                    break
                got.append(product.id)
                await scope.products.touch_checked(product.id, NOW)
        return got

    assert await drain(bounded=True) == [never, old, past, edge]
    assert await drain(bounded=False) == [no_min, no_max]


async def test_next_due_after_button_read(database: Database, connection: AsyncConnection) -> None:
    """Т75: «Узнать цены» пишет `last_checked_at`, и следующая проверка сдвигается."""
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    bounded = await insert_product(connection, tenant, shop, last_checked_at=NOW)
    unbounded = await insert_product(connection, tenant, shop, min_price=None, last_checked_at=NOW)

    async def due(*, bounded_flag: bool, seconds: int) -> int | None:
        async with database.tenant(_t(tenant)) as scope:
            product = await scope.products.next_due(
                shop,
                now=NOW + timedelta(seconds=seconds),
                recheck_seconds=60,
                unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
                bounded=bounded_flag,
            )
        return None if product is None else product.id

    assert await due(bounded_flag=True, seconds=59) is None
    assert await due(bounded_flag=True, seconds=60) == bounded
    assert await due(bounded_flag=False, seconds=599) is None
    assert await due(bounded_flag=False, seconds=600) == unbounded


async def test_next_due_ignores_other_tenant(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await insert_product(connection, tenant, shop)
    async with database.tenant(_t(other)) as scope:
        for bounded in (True, False):
            assert (
                await scope.products.next_due(
                    shop,
                    now=NOW,
                    recheck_seconds=60,
                    unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
                    bounded=bounded,
                )
                is None
            )


async def test_product_observation_refusal_and_isolation(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    async with database.tenant(_t(tenant)) as scope:
        await scope.products.record_observation(
            product,
            current_price=100,
            position=2,
            position_over=None,
            competitors_count=3,
            first_place_price=90,
            decision_reason=None,
            decision_target_price=None,
            checked_at=NOW,
        )
        await scope.products.set_refusal(
            product,
            reason="stock_counted",
            since=NOW,
            fingerprint="fp",
            retry_at=NOW + timedelta(seconds=1800),
        )
        await scope.commit()
        loaded = await scope.products.get(product)
        assert loaded is not None
        assert (loaded.current_price, loaded.position, loaded.competitors_count) == (100, 2, 3)
        assert (loaded.first_place_price, loaded.last_checked_at) == (90, NOW)
        assert (loaded.refusal_reason, loaded.refusal_since) == ("stock_counted", NOW)
        assert (loaded.refusal_fingerprint, loaded.retry_at) == (
            "fp",
            NOW + timedelta(seconds=1800),
        )
        await scope.products.record_observation(
            product,
            current_price=100,
            position=None,
            position_over=20,
            competitors_count=57,
            first_place_price=90,
            decision_reason=None,
            decision_target_price=None,
            checked_at=NOW,
        )
        await scope.commit()
        loaded = await scope.products.get(product)
        assert loaded is not None
        assert (loaded.position, loaded.position_over, loaded.competitors_count) == (None, 20, 57)
        await scope.products.clear_refusal(product)
        await scope.products.touch_checked(product, NOW + timedelta(seconds=5))
        await scope.commit()
        loaded = await scope.products.get(product)
        assert loaded is not None
        assert loaded.refusal_reason is None
        assert loaded.refusal_since is None
        assert loaded.refusal_fingerprint is None
        assert loaded.retry_at is None
        assert loaded.last_checked_at == NOW + timedelta(seconds=5)
    async with database.tenant(_t(other)) as scope:
        with pytest.raises(NotFoundError):
            await scope.products.touch_checked(product, NOW)
        with pytest.raises(NotFoundError):
            await scope.products.clear_refusal(product)
        with pytest.raises(NotFoundError):
            await scope.products.set_refusal(
                product, reason="r", since=None, fingerprint=None, retry_at=None
            )
        with pytest.raises(NotFoundError):
            await scope.products.record_observation(
                product,
                current_price=1,
                position=1,
                position_over=None,
                competitors_count=0,
                first_place_price=1,
                decision_reason=None,
                decision_target_price=None,
                checked_at=NOW,
            )


# --- Т8: серии ошибок и остановка ---


@pytest.fixture
async def committed(database_url: str, cipher: SecretCipher) -> AsyncIterator[Database]:
    db = create_database(database_url, cipher=cipher)
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        yield db
    finally:
        await db.dispose()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenant"))
        await engine.dispose()


async def _committed_shop(database: Database, database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    engine = create_async_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            tenant = await insert_tenant(conn)
            shop = await insert_shop(conn, tenant)
        return tenant, shop
    finally:
        await engine.dispose()


async def test_parallel_write_errors_are_counted_atomically(
    committed: Database, database_url: str
) -> None:
    tenant, shop = await _committed_shop(committed, database_url)

    async def one() -> None:
        async with committed.tenant(_t(tenant)) as scope:
            await scope.shops.record_write_outcome(shop, ok=False, limit=3, now=NOW)
            await scope.commit()

    await asyncio.gather(*[one() for _ in range(20)])
    async with committed.tenant(_t(tenant)) as scope:
        loaded = await scope.shops.get(shop)
        assert loaded is not None
        assert loaded.write_error_streak == 20
        assert loaded.halt_reason == "write_errors"
        assert loaded.login_error_streak == 0


@pytest.mark.parametrize(
    ("method", "reason", "column"),
    [
        ("record_write_outcome", "write_errors", "write_error_streak"),
        ("record_login_outcome", "login_errors", "login_error_streak"),
    ],
)
async def test_streak_halts_on_third_and_resets_on_success(
    method: str, reason: str, column: str, database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    async with database.tenant(_t(tenant)) as scope:
        record = getattr(scope.shops, method)
        for step in (1, 2):
            loaded = await record(shop, ok=False, limit=3, now=NOW + timedelta(seconds=step))
            assert getattr(loaded, column) == step
            assert loaded.halt_reason is None
            assert loaded.halted_at is None
        third = await record(shop, ok=False, limit=3, now=NOW + timedelta(seconds=3))
        assert (third.halt_reason, third.halted_at) == (reason, NOW + timedelta(seconds=3))
        fourth = await record(shop, ok=False, limit=3, now=NOW + timedelta(seconds=4))
        assert getattr(fourth, column) == 4
        assert (fourth.halt_reason, fourth.halted_at) == (reason, NOW + timedelta(seconds=3))
        ok = await record(shop, ok=True, limit=3, now=NOW + timedelta(seconds=5))
        assert getattr(ok, column) == 0
        assert ok.halt_reason == reason  # успех остановку не снимает


async def test_success_resets_series_before_limit(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    async with database.tenant(_t(tenant)) as scope:
        for ok in (False, False, True, False, False):
            loaded = await scope.shops.record_write_outcome(shop, ok=ok, limit=3, now=NOW)
        assert loaded.write_error_streak == 2
        assert loaded.halt_reason is None


async def test_halt_and_clear_halt(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    async with database.tenant(_t(tenant)) as scope:
        await scope.shops.record_write_outcome(shop, ok=False, limit=9, now=NOW)
        await scope.shops.record_login_outcome(shop, ok=False, limit=9, now=NOW)
        halted = await scope.shops.halt(shop, "store_anomalies", NOW)
        assert (halted.halt_reason, halted.halted_at) == ("store_anomalies", NOW)
        cleared = await scope.shops.clear_halt(shop)
        assert cleared.halt_reason is None
        assert cleared.halted_at is None
        assert (cleared.login_error_streak, cleared.write_error_streak) == (0, 0)
    async with database.tenant(_t(other)) as scope:
        for call in (
            scope.shops.halt(shop, "x", NOW),
            scope.shops.clear_halt(shop),
            scope.shops.record_write_outcome(shop, ok=False, limit=3, now=NOW),
            scope.shops.record_login_outcome(shop, ok=False, limit=3, now=NOW),
            scope.shops.lock_for_write(shop),
        ):
            with pytest.raises(NotFoundError):
                await call


async def test_lock_for_write_returns_fresh_shop(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, bot_mode="live")
    async with database.tenant(_t(tenant)) as scope:
        before = await scope.shops.get(shop)
        assert before is not None and before.bot_mode == "live"
        await connection.execute(
            text("UPDATE shop SET bot_mode = 'off' WHERE id = :i"), {"i": shop}
        )
        locked = await scope.shops.lock_for_write(shop)
        assert locked.bot_mode == "off"


# --- Т9: price_write ---


async def _open(scope, product: int, change: int, *, sent_at, now):  # type: ignore[no-untyped-def]
    return await scope.price_writes.open(
        product,
        snapshot={"state": {}, "listing": {}},
        expected={"710000000": 90},
        sent_at=sent_at,
        price_change_id=change,
        now=now,
    )


@pytest.mark.parametrize("status", ["sending", "receipt", "unknown"])
async def test_open_refused_while_hold_active(
    status: str, database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product, status=status, sent_at=ago(599))
    change = await insert_change(connection, tenant, shop, product)
    async with database.tenant(_t(tenant)) as scope:
        assert await _open(scope, product, change, sent_at=NOW, now=NOW) is False
        row = await scope.price_writes.get(product)
        assert row is not None
        assert (row.status, row.sent_at) == (status, ago(599))


@pytest.mark.parametrize("status", ["sending", "receipt", "unknown", "confirmed", "expired"])
async def test_open_replaces_when_hold_over_or_not_blocking(
    status: str, database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    age = 600 if status in {"sending", "receipt", "unknown"} else 10
    await insert_write(connection, tenant, shop, product, status=status, sent_at=ago(age))
    change = await insert_change(connection, tenant, shop, product)
    async with database.tenant(_t(tenant)) as scope:
        assert await _open(scope, product, change, sent_at=NOW, now=NOW) is True
        await scope.commit()
        row = await scope.price_writes.get(product)
        assert row is not None
        assert row.status == "sending"
        assert row.sent_at == NOW
        assert row.price_change_id == change
        assert row.expected_prices == {"710000000": 90}
        assert row.next_verify_at == NOW + timedelta(seconds=30)
        assert row.deadline_reported is False
        assert row.error_counted is False
        assert row.sku == "sku" or row.sku.startswith("sku-")


async def test_open_inserts_new_row(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    change = await insert_change(connection, tenant, shop, product)
    async with database.tenant(_t(tenant)) as scope:
        assert await _open(scope, product, change, sent_at=NOW, now=NOW) is True
        await scope.commit()
        row = await scope.price_writes.get(product)
        assert row is not None
        assert row.shop_id == shop


async def test_open_other_tenant_not_found(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    change = await insert_change(connection, tenant, shop, product)
    async with database.tenant(_t(other)) as scope:
        with pytest.raises(NotFoundError):
            await _open(scope, product, change, sent_at=NOW, now=NOW)
        assert await scope.price_writes.get(product) is None
        assert await scope.price_writes.due_for_verification(shop, NOW) == []
        assert await scope.price_writes.delete(product) is False


async def test_set_status_delete_and_due(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    products = [await insert_product(connection, tenant, shop) for _ in range(3)]
    for product, nv in zip(products, (ago(10), NOW, NOW + timedelta(seconds=1)), strict=True):
        await insert_write(connection, tenant, shop, product, next_verify_at=nv)
    async with database.tenant(_t(tenant)) as scope:
        due = await scope.price_writes.due_for_verification(shop, NOW)
        assert [w.product_id for w in due] == [products[0], products[1]]

        await scope.price_writes.set_status(products[0], "unknown", job_id="j1")
        row = await scope.price_writes.get(products[0])
        assert row is not None
        assert (row.status, row.job_id, row.next_verify_at) == ("unknown", "j1", ago(10))
        later = NOW + timedelta(seconds=30)
        await scope.price_writes.set_status(
            products[0], "unknown", next_verify_at=later, deadline_reported=True, error_counted=True
        )
        await scope.price_writes.set_status(products[1], "confirmed", next_verify_at=None)
        await scope.commit()
        row = await scope.price_writes.get(products[0])
        assert row is not None
        assert (row.next_verify_at, row.deadline_reported, row.job_id) == (later, True, "j1")
        assert row.error_counted is True
        row = await scope.price_writes.get(products[1])
        assert row is not None
        assert row.next_verify_at is None
        assert await scope.price_writes.due_for_verification(shop, NOW) == []

        assert await scope.price_writes.delete(products[2]) is True
        assert await scope.price_writes.get(products[2]) is None
        assert await scope.price_writes.delete(products[2]) is False
        with pytest.raises(NotFoundError):
            await scope.price_writes.set_status(products[2], "unknown")


async def test_next_verify_time_is_the_earliest_open_check_of_the_shop(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    other_shop = await insert_shop(connection, other)
    products = [await insert_product(connection, tenant, shop) for _ in range(3)]
    foreign = await insert_product(connection, other, other_shop)
    soon = NOW + timedelta(seconds=30)
    for product, nv in zip(products, (NOW + timedelta(seconds=90), soon, None), strict=True):
        await insert_write(connection, tenant, shop, product, next_verify_at=nv)
    await insert_write(
        connection, other, other_shop, foreign, next_verify_at=NOW + timedelta(seconds=1)
    )
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.price_writes.next_verify_time(shop) == soon
        assert await scope.price_writes.next_verify_time(other_shop) is None  # чужой магазин
        await scope.price_writes.set_status(products[1], "confirmed", next_verify_at=None)
        assert await scope.price_writes.next_verify_time(shop) == NOW + timedelta(seconds=90)
        await scope.price_writes.delete(products[0])
        assert await scope.price_writes.next_verify_time(shop) is None


def test_open_defaults_mirror_kaspi_constants() -> None:
    import inspect

    from kaspisa.db import PriceWriteRepository
    from kaspisa.kaspi import PRICE_VERIFY_FIRST_CHECK_SECONDS, PRICE_WRITE_HOLD_SECONDS

    params = inspect.signature(PriceWriteRepository.open).parameters
    assert params["hold_seconds"].default == PRICE_WRITE_HOLD_SECONDS
    assert params["first_check_seconds"].default == PRICE_VERIFY_FIRST_CHECK_SECONDS


# --- Т10, Т11, Т20: журнал цен ---


async def test_count_sent_since(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    for result, age in (
        ("applied", 10),
        ("unconfirmed", 20),
        ("failed", 30),
        ("dry_run", 40),
        ("applied", 1800),
        ("unconfirmed", 1799),
    ):
        await insert_change(connection, tenant, shop, product, result=result, created_at=ago(age))
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.price_changes.count_sent_since(shop, ago(1800)) == 3
    async with database.tenant(_t(other)) as scope:
        assert await scope.price_changes.count_sent_since(shop, ago(1800)) == 0


async def test_set_result_and_created_at(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    async with database.tenant(_t(tenant)) as scope:
        change = await scope.price_changes.add(
            product_id=product,
            old_price=100,
            new_price=90,
            reason="undercut_first",
            result=PriceChangeResult.UNCONFIRMED,
            created_at=ago(77),
        )
        await scope.commit()
        assert change.created_at == ago(77)
        await scope.price_changes.set_result(
            change.id, PriceChangeResult.FAILED, error="not_confirmed"
        )
        await scope.commit()
        listed = await scope.price_changes.list_for_product(product, limit=5)
        assert [(c.result, c.error, c.created_at) for c in listed] == [
            ("failed", "not_confirmed", ago(77))
        ]
        # время по умолчанию ставит база
        auto = await scope.price_changes.add(
            product_id=product,
            old_price=100,
            new_price=95,
            reason="r",
            result=PriceChangeResult.DRY_RUN,
        )
        assert auto.created_at is not None
    async with database.tenant(_t(other)) as scope:
        with pytest.raises(NotFoundError):
            await scope.price_changes.set_result(change.id, PriceChangeResult.APPLIED, error=None)


async def test_discard_unsent(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    unconfirmed = await insert_change(connection, tenant, shop, product, result="unconfirmed")
    applied = await insert_change(connection, tenant, shop, product, result="applied")
    async with database.tenant(_t(other)) as scope:
        assert await scope.price_changes.discard_unsent(unconfirmed) is False
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.price_changes.discard_unsent(applied) is False
        assert await scope.price_changes.discard_unsent(unconfirmed) is True
        assert await scope.price_changes.discard_unsent(unconfirmed) is False
        await scope.commit()
        ids = [c.id for c in await scope.price_changes.list_for_product(product, limit=10)]
        assert ids == [applied]


@pytest.mark.parametrize(
    ("ages", "expected_shift"),
    [
        ([1700] * 249, None),
        ([1700] * 250, 100),
        ([1700, 1600] + [1500] * 249, 200),
        ([1800] + [1500] * 249, None),  # строка на границе окна не считается
    ],
)
async def test_quota_free_at(
    ages: list[int],
    expected_shift: int | None,
    database: Database,
    connection: AsyncConnection,
) -> None:
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    for age in ages:
        await insert_change(connection, tenant, shop, product, created_at=ago(age))
    await insert_change(connection, tenant, shop, product, result="failed", created_at=ago(1700))
    await insert_change(connection, tenant, shop, product, result="dry_run", created_at=ago(1700))
    async with database.tenant(_t(tenant)) as scope:
        got = await scope.price_changes.quota_free_at(shop, now=NOW, limit=250, window_seconds=1800)
    expected = None if expected_shift is None else NOW + timedelta(seconds=expected_shift)
    assert got == expected


# --- Т12: автовход ---


async def test_mark_connected_auto_login_sets_unproven_version(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    async with database.tenant(_t(tenant.id)) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid="m-1",
            session_state=Secret("s"),
            expected_version=shop.connection_version,
        )
        assert shop.unproven_login_version is None
        shop = await scope.shops.mark_reauth_required(
            shop.id, error="session_expired", expected_version=shop.connection_version
        )
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid="m-1",
            session_state=Secret("s2"),
            expected_version=shop.connection_version,
            auto_login=True,
        )
        assert shop.unproven_login_version == shop.connection_version

        # вход продавца сбрасывает признак
        shop = await scope.shops.mark_reauth_required(
            shop.id, error="session_expired", expected_version=shop.connection_version
        )
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid="m-1",
            session_state=Secret("s3"),
            expected_version=shop.connection_version,
        )
        assert shop.unproven_login_version is None


async def test_clear_unproven_login_needs_matching_version(database: Database) -> None:
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    async with database.tenant(_t(tenant.id)) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid="m-1",
            session_state=Secret("s"),
            expected_version=shop.connection_version,
            auto_login=True,
        )
        version = shop.unproven_login_version
        assert version is not None
        await scope.shops.clear_unproven_login(shop.id, version + 5)
        again = await scope.shops.get(shop.id)
        assert again is not None
        await scope.commit()
        reloaded = await scope.shops.lock_for_write(shop.id)
        assert reloaded.unproven_login_version == version
        await scope.shops.clear_unproven_login(shop.id, version)
        await scope.commit()
        reloaded = await scope.shops.lock_for_write(shop.id)
        assert reloaded.unproven_login_version is None


# --- изоляция арендаторов и свежесть объектов внутри одной области ---


async def test_clear_unproven_login_other_tenant(
    database: Database, connection: AsyncConnection
) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    await connection.execute(
        text("UPDATE shop SET unproven_login_version = 3 WHERE id = :i"), {"i": shop}
    )
    async with database.tenant(_t(other)) as scope:
        assert await scope.shops.clear_unproven_login(shop, 3) is False
    async with database.tenant(_t(tenant)) as scope:
        assert (await scope.shops.lock_for_write(shop)).unproven_login_version == 3


async def test_quota_free_at_other_tenant(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    for _ in range(250):
        await insert_change(connection, tenant, shop, product, created_at=ago(100))
    async with database.tenant(_t(tenant)) as scope:
        assert await scope.price_changes.quota_free_at(
            shop, now=NOW, limit=250, window_seconds=1800
        ) == NOW + timedelta(seconds=1700)
    async with database.tenant(_t(other)) as scope:
        assert (
            await scope.price_changes.quota_free_at(shop, now=NOW, limit=250, window_seconds=1800)
            is None
        )


async def test_set_status_other_tenant(database: Database, connection: AsyncConnection) -> None:
    tenant = await insert_tenant(connection)
    other = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    product = await insert_product(connection, tenant, shop)
    await insert_write(connection, tenant, shop, product, status="receipt")
    async with database.tenant(_t(other)) as scope:
        with pytest.raises(NotFoundError):
            await scope.price_writes.set_status(product, "confirmed")
    async with database.tenant(_t(tenant)) as scope:
        row = await scope.price_writes.get(product)
        assert row is not None
        assert row.status == "receipt"


async def test_loaded_shop_is_fresh_after_lease_and_unproven_updates(
    database: Database, connection: AsyncConnection
) -> None:
    """Объект магазина, загруженный раньше, видит новые значения после обновлений аренды."""
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant, claimed_by="w1", claimed_until=NOW)
    await connection.execute(
        text("UPDATE shop SET unproven_login_version = 3 WHERE id = :i"), {"i": shop}
    )
    until = NOW + timedelta(seconds=600)
    async with database.tenant(_t(tenant)) as scope:
        loaded = await scope.shops.get(shop)
        assert loaded is not None
        assert await scope.shops.defer(shop, "w1", until) is True
        assert await scope.shops.clear_unproven_login(shop, 3) is True
        await scope.commit()
        again = await scope.shops.get(shop)
        assert again is not None
        assert again.claimed_until == until
        assert again.unproven_login_version is None
        assert await scope.shops.release_lease(shop, "w1") is True
        await scope.commit()
        final = await scope.shops.get(shop)
        assert final is not None
        assert final.claimed_by is None
        assert final.claimed_until is None
