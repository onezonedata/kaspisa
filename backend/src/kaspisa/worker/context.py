"""Контексты обработчика и узкий интерфейс клиента Kaspi."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from redis.asyncio import Redis

from kaspisa.db import Database, Shop, TenantId
from kaspisa.kaspi import KaspiSession
from kaspisa.kaspi.models import (
    CodeRequired,
    KaspiCredentials,
    ListedProduct,
    LoggedIn,
    OffersPage,
    PriceVerification,
    PriceWriteReceipt,
    ProductSnapshot,
    ProductState,
)
from kaspisa.worker.clock import Clock, utc_now
from kaspisa.worker.redis_limits import RedisIntervalLimiter

if TYPE_CHECKING:
    from kaspisa.worker.access import ShopAccess


class KaspiApi(Protocol):
    """То, что обработчик берёт у клиента Kaspi (настоящий клиент и заглушка тестов)."""

    async def find_product(
        self, session: KaspiSession, merchant_uid: str, sku: str
    ) -> ListedProduct | None: ...

    async def list_products(
        self, session: KaspiSession, merchant_uid: str
    ) -> list[ListedProduct]: ...

    async def get_offers(self, master_sku: str, city_id: str, merchant_uid: str) -> OffersPage: ...

    async def get_product(
        self, session: KaspiSession, merchant_uid: str, sku: str
    ) -> ProductState: ...

    async def set_price(
        self, session: KaspiSession, product: ProductSnapshot, city_prices: Mapping[str, int]
    ) -> PriceWriteReceipt: ...

    async def verify_price(
        self,
        session: KaspiSession,
        product: ProductSnapshot,
        expected: Mapping[str, int],
        *,
        settled: bool = False,
    ) -> PriceVerification: ...

    async def login(
        self, credentials: KaspiCredentials, previous: KaspiSession | None = None
    ) -> LoggedIn | CodeRequired: ...


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    dry_run: bool
    slots: int


@dataclass(frozen=True)
class WorkerContext:
    database: Database
    client: KaspiApi
    redis: Redis
    limiter: RedisIntervalLimiter
    config: WorkerConfig
    clock: Clock = utc_now


@dataclass
class SliceContext:
    """Один захват магазина: арендатор и доступ (магазин и сессия кабинета)."""

    worker: WorkerContext
    tenant_id: TenantId
    access: ShopAccess

    @property
    def shop(self) -> Shop:
        return self.access.shop
