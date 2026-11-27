"""Общие помощники тестов обработчика: Redis, база, управляемые часы, заглушка клиента Kaspi.

Фикстуры не в conftest.py (второй conftest ломает mypy): тесты импортируют их явно.
Копия работы с тестовой базой из tests/db/db_testing.py: из другого каталога тестов
этот модуль не импортируется.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from kaspisa.db import Database, TenantId, create_database
from kaspisa.kaspi.models import (
    CodeRequired,
    KaspiCredentials,
    ListedProduct,
    LoggedIn,
    Offer,
    OffersPage,
    PriceVerification,
    PriceWriteReceipt,
    ProductSnapshot,
    ProductState,
)
from kaspisa.kaspi.session import KaspiSession
from kaspisa.security import KeyRing, Secret, SecretCipher

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic" / "alembic.ini"
NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
CITY_ID = "750000000"


# --- часы ---


class FakeClock:
    """Управляемые часы: `datetime` для обработчика и секунды для ограничителя."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def seconds(self) -> float:
        return self.now.timestamp()


# --- Postgres ---


def _server() -> tuple[str, int, str, str]:
    return "localhost", int(os.environ.get("KASPISA_PG_PORT", "5432")), "kaspisa", "kaspisa"


def _url(name: str) -> str:
    host, port, user, password = _server()
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


async def _admin(sql: str) -> None:
    engine = create_async_engine(
        _url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def worker_database_url() -> Iterator[str]:
    """База прогона с миграциями до head; в конце удаляется."""
    name = f"kaspisa_test_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config(str(ALEMBIC_INI))
        config.set_main_option("sqlalchemy.url", _url(name))
        command.upgrade(config, "head")
        yield _url(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def worker_cipher() -> SecretCipher:
    return SecretCipher(KeyRing(1, {1: os.urandom(32)}))


@dataclass
class Rig:
    """База с настоящими commit; в конце теста арендаторы удаляются каскадом."""

    url: str
    cipher: SecretCipher
    engine: AsyncEngine
    databases: list[Database]

    def new_database(self) -> Database:
        db = create_database(self.url, cipher=self.cipher)
        self.databases.append(db)
        return db


@pytest.fixture
async def rig(worker_database_url: str, worker_cipher: SecretCipher) -> AsyncIterator[Rig]:
    engine = create_async_engine(worker_database_url, poolclass=NullPool, hide_parameters=True)
    state = Rig(worker_database_url, worker_cipher, engine, [])
    try:
        yield state
    finally:
        for db in state.databases:
            await db.dispose()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenant"))
        await engine.dispose()


def session_blob() -> Secret:
    """Блок сессии кабинета (выдуманные значения cookie)."""
    cookies = [
        {
            "name": name,
            "value": f"marker-{name}",
            "domain": "mc.shop.kaspi.kz",
            "path": "/",
            "secure": True,
            "expires": None,
            "host_only": True,
        }
        for name in ("mc-session", "mc-sid")
    ]
    return Secret(
        json.dumps({"v": 1, "phase": "authenticated", "pending_login": None, "cookies": cookies})
    )


def authenticated_session() -> KaspiSession:
    return KaspiSession.load(session_blob())


@dataclass(frozen=True)
class SeededShop:
    tenant_id: TenantId
    shop_id: uuid.UUID
    merchant_uid: str
    skus: list[str]
    product_ids: list[int]


async def seed_shop(
    rig: Rig,
    database: Database,
    *,
    products: int = 1,
    last_checked: list[datetime | None] | None = None,
    bot_mode: str = "live",
    skus: list[str] | None = None,
    catalog_synced_at: datetime | None = NOW,
) -> SeededShop:
    """Подключённый магазин с отслеживаемыми товарами (границы 50–500).

    Каталог по умолчанию «загружен только что»: суточная загрузка не мешает прочим тестам.
    """
    async with database.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    uid = f"m-{secrets.token_hex(5)}"
    async with database.tenant(tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid=uid,
            session_state=session_blob(),
            expected_version=shop.connection_version,
        )
        await scope.commit()
    made: list[str] = []
    ids: list[int] = []
    async with rig.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE shop SET city_id = :c, bot_mode = :b, catalog_synced_at = :s WHERE id = :i"
            ),
            {"c": CITY_ID, "b": bot_mode, "i": shop.id, "s": catalog_synced_at},
        )
        for index in range(products):
            sku = skus[index] if skus else f"sku-{secrets.token_hex(4)}"
            checked = last_checked[index] if last_checked else None
            row = await conn.execute(
                text(
                    "INSERT INTO product (tenant_id, shop_id, sku, master_sku, title, step, "
                    "tracked, auto_decrease, min_price, max_price, last_checked_at) VALUES "
                    "(:t, :s, :sku, :m, 'Товар', 1, true, true, 50, 500, :lc) RETURNING id"
                ),
                {"t": tenant_id, "s": shop.id, "sku": sku, "m": f"master-{sku}", "lc": checked},
            )
            made.append(sku)
            ids.append(int(row.scalar_one()))
    return SeededShop(tenant_id, shop.id, uid, made, ids)


# --- заглушка клиента Kaspi ---


def listed_product(
    sku: str,
    merchant_uid: str,
    price: int,
    stock_count: int | None = None,
    extra_cities: Mapping[str, int] | None = None,
    cities: Mapping[str, int] | None = None,
    extra_stores: Mapping[str, int | None] | None = None,
) -> ListedProduct:
    """Строка списка; `cities` - все города строки (вместо города сверки и `extra_cities`).

    `extra_stores` - точки кроме `S1`: код -> остаток (`None` - «yes, не указан»).
    """
    if cities is None:
        cities = {CITY_ID: price, **(extra_cities or {})}
    return ListedProduct.model_validate(
        {
            "sku": sku,
            "masterSku": f"master-{sku}",
            "merchantUid": merchant_uid,
            "available": True,
            "archivedAt": None,
            "cityPrices": [{"cityId": city, "value": value} for city, value in cities.items()],
            "availabilities": [
                {
                    "storeId": "S1",
                    "available": "yes",
                    "stockCount": stock_count,
                    "stockSpecified": stock_count is not None,
                    "preOrder": 0,
                },
                *(
                    {
                        "storeId": store,
                        "available": "yes",
                        "stockCount": count,
                        "stockSpecified": count is not None,
                        "preOrder": 0,
                    }
                    for store, count in (extra_stores or {}).items()
                ),
            ],
        }
    )


def session_with_phase(phase: str, pending_login: str | None = None) -> KaspiSession:
    """Сессия в сохраняемой фазе (для `LoggedIn` и `CodeRequired`)."""
    data = json.loads(session_blob().reveal())
    data["phase"] = phase
    data["pending_login"] = pending_login
    return KaspiSession.load(Secret(json.dumps(data)))


def logged_in() -> LoggedIn:
    return LoggedIn(session=session_with_phase("authenticated"), merchants=[])


def code_required() -> CodeRequired:
    return CodeRequired(session=session_with_phase("awaiting_code", "a@b.kz"))


class Unset:
    """Тип метки «значение не задано» (отличается от `None` - «остаток не указан»)."""


UNSET = Unset()


class FakeKaspiClient:
    """Записывает вызовы и их порядок; ответы настраиваются полями."""

    def __init__(
        self,
        *,
        price: int = 100,
        competitor_price: int = 95,
        call_seconds: float = 0.0,
        clock: FakeClock | None = None,
        pause: float = 0.0,
    ) -> None:
        self.price = price
        self.competitor_price = competitor_price
        self.call_seconds = call_seconds
        self.clock = clock
        self.pause = pause
        self.stock_count: int | None = None
        self.extra_stocks: dict[str, int | None] = {}  # прочие точки чтения: код -> остаток
        # остаток `S1` в строке списка отдельно от чтения; `UNSET` - как `stock_count`
        self.listed_stock_count: int | Unset | None = UNSET
        self.extra_cities: dict[str, int] = {}  # города товара кроме города сверки: id -> цена
        # полный набор городов строки (id -> цена) вместо города сверки и `extra_cities`
        self.cities: dict[str, int] | None = None
        self.own_offer: int | None = None  # цена своего предложения в карточке; None - `price`
        self.own_in_offers = True  # есть ли своё предложение в карточке
        self.offers_cities: list[str] = []  # города, по которым запрошены предложения
        self.extra_offers: dict[str, int] = {}  # прочие продавцы карточки: merchant_id -> цена
        self.calls: list[tuple[str, str]] = []
        self.spans: list[tuple[str, float, float]] = []
        self.errors: dict[str, BaseException] = {}
        self.on_call: Callable[[str, str], Awaitable[None]] | None = None
        self.merchant_uid = ""
        # очереди ответов: непустая очередь отдаёт первый элемент, пустая - ответ по умолчанию
        self.find_results: list[ListedProduct | BaseException | None] = []
        self.verify_results: list[PriceVerification | BaseException] = []
        self.login_results: list[LoggedIn | CodeRequired | BaseException] = []
        self.set_price_gate: asyncio.Event | None = None
        self.session_changed = False
        self.sent: list[tuple[ProductSnapshot, dict[str, int]]] = []
        self.verified: list[tuple[ProductSnapshot, dict[str, int]]] = []
        self.verified_settled: list[bool] = []  # параметр `settled` каждой проверки (этап 23)
        self.credentials: list[KaspiCredentials] = []
        self.catalog: list[ListedProduct] = []  # ответ `list_products`
        self.offers_page: OffersPage | None = None  # ответ `get_offers` вместо умолчания

    async def _enter(self, method: str, key: str) -> None:
        self.calls.append((method, key))
        if self.on_call is not None:
            await self.on_call(method, key)
        start = asyncio.get_running_loop().time()
        if self.pause:
            await asyncio.sleep(self.pause)
        if self.clock is not None and self.call_seconds:
            self.clock.advance(self.call_seconds)
        self.spans.append((key, start, asyncio.get_running_loop().time()))
        if method in self.errors:
            raise self.errors[method]

    def _touch(self, session: KaspiSession) -> None:
        if self.session_changed:
            session.changed = True

    async def find_product(
        self, session: KaspiSession, merchant_uid: str, sku: str
    ) -> ListedProduct | None:
        self.merchant_uid = merchant_uid
        await self._enter("find_product", sku)
        self._touch(session)
        if self.find_results:
            item = self.find_results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        listed = (
            self.stock_count
            if isinstance(self.listed_stock_count, Unset)
            else (self.listed_stock_count)
        )
        return listed_product(
            sku, merchant_uid, self.price, listed, self.extra_cities, self.row_cities()
        )

    def row_cities(self) -> dict[str, int]:
        """Города строки списка и чтения товара: id -> цена."""
        if self.cities is not None:
            return dict(self.cities)
        return {CITY_ID: self.price, **self.extra_cities}

    async def list_products(self, session: KaspiSession, merchant_uid: str) -> list[ListedProduct]:
        await self._enter("list_products", merchant_uid)
        self._touch(session)
        return list(self.catalog)

    async def get_offers(self, master_sku: str, city_id: str, merchant_uid: str) -> OffersPage:
        self.offers_cities.append(city_id)
        await self._enter("get_offers", master_sku)
        if self.offers_page is not None:
            return self.offers_page
        own = self.price if self.own_offer is None else self.own_offer
        offers = [
            Offer.model_validate(
                {"merchantId": "other", "merchantName": "Чужой", "price": self.competitor_price}
            ),
            *(
                Offer.model_validate({"merchantId": m, "merchantName": m, "price": price})
                for m, price in self.extra_offers.items()
            ),
        ]
        if self.own_in_offers:
            offers.insert(
                0,
                Offer.model_validate(
                    {"merchantId": self.merchant_uid, "merchantName": "Свой", "price": own}
                ),
            )
        return OffersPage(offers=offers, total=len(offers))

    async def get_product(self, session: KaspiSession, merchant_uid: str, sku: str) -> ProductState:
        await self._enter("get_product", sku)
        self._touch(session)
        return ProductState.model_validate(
            {
                "sku": sku,
                "masterSku": f"master-{sku}",
                "title": "Товар",
                "available": True,
                "cities": [
                    {
                        "id": city,
                        "price": value,
                        "points": [
                            {"id": "S1", "stock": self.stock_count},
                            *({"id": store, "stock": n} for store, n in self.extra_stocks.items()),
                        ],
                    }
                    for city, value in self.row_cities().items()
                ],
            }
        )

    async def set_price(
        self,
        session: KaspiSession,
        product: ProductSnapshot,
        city_prices: Mapping[str, int],
    ) -> PriceWriteReceipt:
        self.sent.append((product, dict(city_prices)))
        await self._enter("set_price", product.state.sku)
        if self.set_price_gate is not None:
            await self.set_price_gate.wait()
        return PriceWriteReceipt.model_validate({"id": "job-1"})

    async def verify_price(
        self,
        session: KaspiSession,
        product: ProductSnapshot,
        expected: Mapping[str, int],
        *,
        settled: bool = False,
    ) -> PriceVerification:
        self.verified.append((product, dict(expected)))
        self.verified_settled.append(settled)
        await self._enter("verify_price", product.state.sku)
        self._touch(session)
        if self.verify_results:
            item = self.verify_results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return PriceVerification(applied=True, listing=None, mismatches={}, store_anomalies=())

    async def login(
        self, credentials: KaspiCredentials, previous: KaspiSession | None = None
    ) -> LoggedIn | CodeRequired:
        self.credentials.append(credentials)
        await self._enter("login", "")
        if self.login_results:
            item = self.login_results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return logged_in()

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def not_applied() -> PriceVerification:
    return PriceVerification(applied=False, listing=None, mismatches={}, store_anomalies=())


def anomalies(*stores: str) -> PriceVerification:
    return PriceVerification(applied=True, listing=None, mismatches={}, store_anomalies=stores)


def stock_reset(
    applied: bool = True,
    mismatches: dict[str, tuple[int, int | None]] | None = None,
    extra: tuple[str, ...] = (),
    sku: str = "sku",
    merchant_uid: str = "uid",
    price: int = 94,
) -> PriceVerification:
    """Ответ проверки со сбросом остатка: строка списка `S1` «yes, не указан» (точка `S1`)."""
    return PriceVerification(
        applied=applied,
        listing=listed_product(sku, merchant_uid, price, stock_count=None),
        mismatches=mismatches or {},
        store_anomalies=("S1", *extra),
    )


# --- сборка контекстов и чтение базы ---


class Sleeps:
    """Подменённый `sleep` ограничителя: записывает задержки, не спит."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_worker_ctx(
    database: Database,
    client: Any,
    redis: Redis,
    prefix: str,
    clock: FakeClock,
    *,
    dry_run: bool = False,
    worker_id: str = "w1",
    limiter_max_wait: float | None = None,
) -> Any:
    from kaspisa.worker.context import WorkerConfig, WorkerContext
    from kaspisa.worker.redis_limits import RedisIntervalLimiter

    extra: dict[str, Any] = {} if limiter_max_wait is None else {"max_wait": limiter_max_wait}
    limiter = RedisIntervalLimiter(
        redis, prefix=prefix, clock=clock.seconds, sleep=Sleeps(), **extra
    )
    return WorkerContext(
        database=database,
        client=client,
        redis=redis,
        limiter=limiter,
        config=WorkerConfig(worker_id=worker_id, dry_run=dry_run, slots=1),
        clock=clock,
    )


async def open_slice_ctx(ctx: Any, shop: SeededShop) -> Any:
    """Контекст слайса с открытым доступом (магазин `connected`)."""
    from kaspisa.worker.access import ShopAccess
    from kaspisa.worker.context import SliceContext

    access = await ShopAccess.open(ctx, shop.tenant_id, shop.shop_id)
    return SliceContext(ctx, shop.tenant_id, access)


async def run_one_slice(ctx: Any, database: Database, now: datetime | None = None) -> bool:
    """Захват магазина и слайс; False, если захватывать нечего."""
    from kaspisa.worker.constants import (
        LEASE_SECONDS,
        MIN_RECHECK_SECONDS,
        UNBOUNDED_RECHECK_SECONDS,
    )
    from kaspisa.worker.slice import run_shop_slice

    async with database.system() as system:
        claim = await system.claim_shop(
            worker_id=ctx.config.worker_id,
            now=now or ctx.clock(),
            lease_seconds=LEASE_SECONDS,
            recheck_seconds=MIN_RECHECK_SECONDS,
            unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
        )
    if claim is None:
        return False
    await run_shop_slice(ctx, claim)
    return True


async def fetch(rig: Rig, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with rig.engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(text(sql), params)).all()]


async def execute(rig: Rig, sql: str, **params: Any) -> None:
    async with rig.engine.begin() as conn:
        await conn.execute(text(sql), params)


async def shop_row(rig: Rig, shop_id: uuid.UUID) -> dict[str, Any]:
    rows = await fetch(
        rig,
        "SELECT connection_state, connection_error, connection_version, halt_reason, "
        "claimed_by, claimed_until, login_error_streak, write_error_streak, "
        "unproven_login_version FROM shop WHERE id = :i",
        i=shop_id,
    )
    keys = [
        "state",
        "error",
        "version",
        "halt",
        "claimed_by",
        "claimed_until",
        "login_streak",
        "write_streak",
        "unproven",
    ]
    return dict(zip(keys, rows[0], strict=True))


async def journal(rig: Rig, shop_id: uuid.UUID) -> list[tuple[Any, ...]]:
    """Журнал цен магазина: (old, new, reason, result, error), по порядку записи."""
    return await fetch(
        rig,
        "SELECT old_price, new_price, reason, result, error FROM price_change "
        "WHERE shop_id = :i ORDER BY id",
        i=shop_id,
    )


# --- Redis ---


def redis_port() -> int:
    return int(os.environ.get("KASPISA_REDIS_PORT", "6379"))


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis(port=redis_port(), socket_connect_timeout=1, socket_timeout=2)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis недоступен: выполните make up (порт — KASPISA_REDIS_PORT)")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def redis_prefix(redis_client: Redis) -> AsyncIterator[str]:
    """Уникальный префикс ключей теста; ключи удаляются в конце."""
    prefix = f"test:{uuid.uuid4().hex[:10]}"
    try:
        yield prefix
    finally:
        keys: list[Any] = [key async for key in redis_client.scan_iter(f"{prefix}:*")]
        if keys:
            await redis_client.delete(*keys)
