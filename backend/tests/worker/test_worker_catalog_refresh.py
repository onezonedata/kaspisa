"""Н7 (ADR 0010): суточная загрузка каталога обработчиком."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    execute,
    fetch,
    journal,
    listed_product,
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

from kaspisa.db import ClaimedShop
from kaspisa.kaspi import KaspiTransportError, RateLimitedError, SessionExpiredError
from kaspisa.worker import catalog as catalog_module
from kaspisa.worker.constants import (
    CATALOG_LOCK_BUSY_SECONDS,
    CATALOG_REFRESH_SECONDS,
    CATALOG_RETRY_SECONDS,
    LEASE_SECONDS,
)
from kaspisa.worker.redis_limits import LimiterBusyError, LimiterUnavailableError, SyncLock
from kaspisa.worker.slice import run_shop_slice

DAY = 86_400


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


class Setup:
    def __init__(self, rig: Rig, redis: Redis, prefix: str, **seed: Any) -> None:
        self.rig = rig
        self.redis = redis
        self.prefix = prefix
        self.seed = seed
        self.clock = FakeClock()
        self.client = FakeKaspiClient()
        self.database = rig.new_database()

    async def start(self, *, products: int = 0, dry_run: bool = True, **extra: Any) -> Setup:
        self.shop = await seed_shop(
            self.rig, self.database, products=products, **{**self.seed, **extra}
        )
        self.client.merchant_uid = self.shop.merchant_uid
        self.ctx = make_worker_ctx(
            self.database, self.client, self.redis, self.prefix, self.clock, dry_run=dry_run
        )
        return self

    def offer(self, *skus: str) -> None:
        self.client.catalog = [listed_product(sku, self.shop.merchant_uid, 100) for sku in skus]

    async def slice(self, stop: asyncio.Event | None = None) -> None:
        """Слайс захваченного вручную магазина (минуя условия захвата)."""
        claim = ClaimedShop(self.shop.tenant_id, self.shop.shop_id)
        await execute(
            self.rig,
            "UPDATE shop SET claimed_by='w1', claimed_until=:u WHERE id=:i",
            u=at(180),
            i=self.shop.shop_id,
        )
        await run_shop_slice(self.ctx, claim, stop)

    async def products(self) -> dict[str, tuple[Any, ...]]:
        rows = await fetch(
            self.rig,
            "SELECT sku, tracked, min_price, max_price, title FROM product WHERE shop_id=:i",
            i=self.shop.shop_id,
        )
        return {row[0]: row[1:] for row in rows}

    async def marks(self) -> tuple[Any, Any]:
        rows = await fetch(
            self.rig,
            "SELECT catalog_synced_at, catalog_retry_at FROM shop WHERE id=:i",
            i=self.shop.shop_id,
        )
        return rows[0][0], rows[0][1]


@pytest.fixture
async def setup(rig: Rig, redis_client: Redis, redis_prefix: str) -> Setup:
    return Setup(rig, redis_client, redis_prefix, catalog_synced_at=None)


async def test_shop_without_tracked_products_is_claimed_and_loads_catalog(setup: Setup) -> None:
    await setup.start(products=0)
    setup.offer("A", "B")

    assert await run_one_slice(setup.ctx, setup.database)

    assert setup.client.methods() == ["list_products"]
    products = await setup.products()
    assert set(products) == {"A", "B"}
    assert all(row[:3] == (False, None, None) for row in products.values())
    assert await setup.marks() == (NOW, None)
    assert await journal(setup.rig, setup.shop.shop_id) == []
    # загруженный магазин больше не захватывается
    assert not await run_one_slice(setup.ctx, setup.database)


async def test_catalog_is_not_loaded_within_24h_of_manual_sync(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    s = await Setup(rig, redis_client, redis_prefix).start(
        products=1, catalog_synced_at=NOW - timedelta(seconds=DAY - 1)
    )
    s.offer("A")
    assert await run_one_slice(s.ctx, s.database)  # товар к проверке: слайс есть
    assert "list_products" not in s.client.methods()
    assert "find_product" in s.client.methods()
    assert set(await s.products()) == set(s.shop.skus)


async def test_catalog_is_loaded_after_24h(setup: Setup) -> None:
    stale = NOW - timedelta(seconds=CATALOG_REFRESH_SECONDS)
    await setup.start(products=0, catalog_synced_at=stale)
    setup.offer("A")
    assert await run_one_slice(setup.ctx, setup.database)
    assert set(await setup.products()) == {"A"}
    assert (await setup.marks())[0] == NOW


async def test_login_halt_forbids_loading(setup: Setup) -> None:
    await setup.start(products=1)
    await execute(
        setup.rig,
        "UPDATE shop SET halt_reason='login_errors', halted_at=:n WHERE id=:i",
        n=NOW,
        i=setup.shop.shop_id,
    )
    setup.offer("A")
    await setup.slice()
    assert setup.client.methods() == []
    assert await setup.marks() == (None, None)


@pytest.mark.parametrize(
    "change",
    [
        "bot_mode = 'off'",
        "city_id = NULL",
        "halt_reason = 'store_anomalies', halted_at = now()",
        "halt_reason = 'write_errors', halted_at = now()",
    ],
)
async def test_other_stops_do_not_forbid_loading(setup: Setup, change: str) -> None:
    await setup.start(products=1)
    await execute(setup.rig, f"UPDATE shop SET {change} WHERE id=:i", i=setup.shop.shop_id)
    setup.offer("A")

    await setup.slice()

    assert setup.client.methods() == ["list_products"]  # решений нет, каталог загружен
    assert "A" in await setup.products()
    assert (await setup.marks())[0] == NOW


async def test_new_products_are_untracked_and_missing_ones_stay(setup: Setup) -> None:
    await setup.start(products=1)
    kept = setup.shop.skus[0]
    setup.offer("NEW")

    await setup.slice()

    products = await setup.products()
    assert products["NEW"][:3] == (False, None, None)
    assert products[kept][:3] == (True, 50, 500)  # пропавший не удалён, настройки целы
    assert all(c.startswith("sku-") or c == "NEW" for c in products)
    # новый товар не попал в журнал цен
    rows = await fetch(
        setup.rig,
        "SELECT count(*) FROM price_change c JOIN product p ON p.id=c.product_id WHERE p.sku='NEW'",
    )
    assert rows[0][0] == 0


async def test_price_checks_run_before_catalog(setup: Setup) -> None:
    await setup.start(products=2)
    setup.offer("A")
    await run_one_slice(setup.ctx, setup.database)
    methods = setup.client.methods()
    assert methods[-1] == "list_products"
    assert methods.count("find_product") == 2
    assert max(i for i, m in enumerate(methods) if m == "find_product") < methods.index(
        "list_products"
    )


async def test_failure_goes_to_log_and_pause_not_to_error_streak(
    setup: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    await setup.start(products=1)
    setup.client.errors["list_products"] = RuntimeError("SECRET-BODY-123")
    caplog.set_level(logging.DEBUG)

    assert await run_one_slice(setup.ctx, setup.database)

    row = await shop_row(setup.rig, setup.shop.shop_id)
    assert (row["login_streak"], row["write_streak"], row["halt"]) == (0, 0, None)
    assert (row["state"], row["claimed_by"], row["claimed_until"]) == ("connected", None, None)
    assert await setup.marks() == (None, at(CATALOG_RETRY_SECONDS))
    assert "RuntimeError" in caplog.text
    assert "SECRET-BODY-123" not in caplog.text
    assert "find_product" in setup.client.methods()  # проверка цен прошла
    product = await fetch(
        setup.rig,
        "SELECT last_checked_at FROM product WHERE id=:i",
        i=setup.shop.product_ids[0],
    )
    assert product[0][0] is not None
    # пауза после сбоя: на каждом захвате магазин не пробуют
    setup.clock.advance(CATALOG_RETRY_SECONDS - 1)
    setup.client.calls.clear()
    await run_one_slice(setup.ctx, setup.database)
    assert "list_products" not in setup.client.methods()
    setup.clock.advance(1)
    setup.client.calls.clear()
    setup.client.errors.clear()
    setup.offer("A")
    await run_one_slice(setup.ctx, setup.database)
    assert "list_products" in setup.client.methods()
    assert await setup.marks() == (setup.clock.now, None)


async def test_repeated_failures_never_halt_the_bot(setup: Setup) -> None:
    await setup.start(products=0)
    setup.client.errors["list_products"] = KaspiTransportError("list_products", "ReadTimeout")
    for _ in range(5):
        assert await run_one_slice(setup.ctx, setup.database)
        setup.clock.advance(CATALOG_RETRY_SECONDS)
    row = await shop_row(setup.rig, setup.shop.shop_id)
    assert (row["login_streak"], row["write_streak"], row["halt"]) == (0, 0, None)
    assert setup.client.methods().count("list_products") == 5


async def test_busy_limiter_defers_catalog_but_not_price_checks(setup: Setup) -> None:
    await setup.start(products=1)
    setup.client.errors["list_products"] = LimiterBusyError(120)

    assert await run_one_slice(setup.ctx, setup.database)

    methods = setup.client.methods()
    assert methods[:2] == ["find_product", "get_offers"] and methods[-1] == "list_products"
    row = await shop_row(setup.rig, setup.shop.shop_id)
    assert (row["claimed_by"], row["claimed_until"], row["halt"]) == (None, None, None)
    assert await setup.marks() == (None, at(120))


async def test_unavailable_limiter_defers_catalog(setup: Setup) -> None:
    await setup.start(products=0)
    setup.client.errors["list_products"] = LimiterUnavailableError()
    assert await run_one_slice(setup.ctx, setup.database)
    synced, retry = await setup.marks()
    assert synced is None and retry is not None and retry > NOW


async def test_rate_limit_is_a_pause_not_a_shop_error(setup: Setup) -> None:
    await setup.start(products=0)
    setup.client.errors["list_products"] = RateLimitedError("list_products", 30)
    assert await run_one_slice(setup.ctx, setup.database)
    row = await shop_row(setup.rig, setup.shop.shop_id)
    assert (row["state"], row["halt"], row["login_streak"]) == ("connected", None, 0)
    synced, retry = await setup.marks()
    assert synced is None and retry is not None and retry > NOW


async def test_expired_session_marks_reauth_without_login_attempt(setup: Setup) -> None:
    await setup.start(products=0)
    setup.client.errors["list_products"] = SessionExpiredError("list_products")
    assert await run_one_slice(setup.ctx, setup.database)
    row = await shop_row(setup.rig, setup.shop.shop_id)
    assert (row["state"], row["error"]) == ("reauth_required", "session_expired")
    assert (row["login_streak"], row["halt"]) == (0, None)
    assert "login" not in setup.client.methods()


async def test_busy_sync_lock_skips_loading_with_short_pause(setup: Setup) -> None:
    await setup.start(products=1)
    lock = SyncLock(setup.redis, prefix=setup.prefix)
    token = await lock.acquire(setup.shop.shop_id, 60_000)
    assert token is not None
    setup.offer("A")

    assert await run_one_slice(setup.ctx, setup.database)

    assert "list_products" not in setup.client.methods()
    assert "find_product" in setup.client.methods()
    synced, retry = await setup.marks()
    assert synced is None
    assert retry == at(CATALOG_LOCK_BUSY_SECONDS)
    assert CATALOG_LOCK_BUSY_SECONDS < CATALOG_RETRY_SECONDS
    assert await lock.release(setup.shop.shop_id, token)  # чужая блокировка не тронута
    setup.clock.advance(CATALOG_LOCK_BUSY_SECONDS)
    await run_one_slice(setup.ctx, setup.database)
    assert "list_products" in setup.client.methods()
    assert "A" in await setup.products()


async def test_lock_is_released_after_loading(setup: Setup) -> None:
    await setup.start(products=0)
    setup.offer("A")
    await run_one_slice(setup.ctx, setup.database)
    lock = SyncLock(setup.redis, prefix=setup.prefix)
    token = await lock.acquire(setup.shop.shop_id, 1000)
    assert token is not None


async def test_changed_session_is_saved(setup: Setup) -> None:
    await setup.start(products=0)
    setup.client.session_changed = True
    setup.offer("A")
    before = await fetch(
        setup.rig, "SELECT session_enc FROM shop WHERE id=:i", i=setup.shop.shop_id
    )
    await run_one_slice(setup.ctx, setup.database)
    after = await fetch(setup.rig, "SELECT session_enc FROM shop WHERE id=:i", i=setup.shop.shop_id)
    assert after != before


async def test_too_large_catalog_is_a_failure_without_changes(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup.start(products=0)
    monkeypatch.setattr(catalog_module, "CATALOG_MAX", 1)
    setup.offer("A", "B")
    await run_one_slice(setup.ctx, setup.database)
    assert await setup.products() == {}
    assert await setup.marks() == (None, at(CATALOG_RETRY_SECONDS))


async def test_stopping_worker_does_not_start_loading(setup: Setup) -> None:
    await setup.start(products=0)
    stop = asyncio.Event()
    stop.set()
    await setup.slice(stop)
    assert setup.client.methods() == []


async def test_reauth_required_shop_does_not_load_catalog(setup: Setup) -> None:
    """Слайс уже идёт, а подключение стало `reauth_required`: загрузки нет, паузы нет."""
    await setup.start(products=0)
    await execute(
        setup.rig,
        "UPDATE shop SET connection_state='reauth_required' WHERE id=:i",
        i=setup.shop.shop_id,
    )
    setup.offer("A")

    await setup.slice()

    assert setup.client.methods() == []
    assert await setup.marks() == (None, None)


async def test_catalog_request_renews_lease(setup: Setup) -> None:
    """Загрузка может идти долго: аренда продлевается до отправки запроса."""
    await setup.start(products=0)
    setup.offer("A")
    seen: list[Any] = []

    async def spy(method: str, key: str) -> None:
        rows = await fetch(
            setup.rig, "SELECT claimed_until FROM shop WHERE id=:i", i=setup.shop.shop_id
        )
        seen.append(rows[0][0])

    setup.client.on_call = spy

    await setup.slice()

    assert seen == [at(LEASE_SECONDS)]  # вручную выставленная аренда была до at(180)


@pytest.mark.parametrize(
    ("retry_after", "pause"),
    [(30, CATALOG_RETRY_SECONDS), (2 * CATALOG_RETRY_SECONDS, 2 * CATALOG_RETRY_SECONDS)],
)
async def test_rate_limit_pause_is_at_least_retry_and_follows_retry_after(
    setup: Setup, retry_after: int, pause: int
) -> None:
    await setup.start(products=0)
    setup.client.errors["list_products"] = RateLimitedError("list_products", retry_after)

    assert await run_one_slice(setup.ctx, setup.database)

    assert await setup.marks() == (None, at(pause))


async def test_session_changed_before_failure_is_still_saved(setup: Setup) -> None:
    """Сбой вне запроса кабинета (не `KaspiError`): cookie из ответа не теряются."""

    class Changing(FakeKaspiClient):
        async def list_products(self, session: Any, merchant_uid: str) -> Any:
            session.changed = True
            raise RuntimeError("boom")

    setup.client = Changing()
    await setup.start(products=0)
    before = await fetch(
        setup.rig, "SELECT session_enc FROM shop WHERE id=:i", i=setup.shop.shop_id
    )

    await run_one_slice(setup.ctx, setup.database)

    after = await fetch(setup.rig, "SELECT session_enc FROM shop WHERE id=:i", i=setup.shop.shop_id)
    assert after != before
    assert (await setup.marks())[1] == at(CATALOG_RETRY_SECONDS)
