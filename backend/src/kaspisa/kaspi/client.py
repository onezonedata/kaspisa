"""Фасад клиента Kaspi."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Self

from kaspisa.kaspi import cabinet, login, public
from kaspisa.kaspi.limiter import IntervalRateLimiter
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
from kaspisa.kaspi.session import KaspiSession
from kaspisa.kaspi.transport import Transport


class KaspiClient:
    """Асинхронный клиент Kaspi. Свой `httpx.AsyncClient` создаёт сам и наружу не выдаёт."""

    def __init__(
        self,
        *,
        limiter: IntervalRateLimiter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transport = Transport(limiter, sleep)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def get_offers(
        self,
        master_sku: str,
        city_id: str,
        merchant_uid: str,
        *,
        page_size: int = public.OFFERS_PAGE_SIZE,
    ) -> OffersPage:
        """Продавцы карточки в городе: одна или две страницы (ADR 0016)."""
        return await public.get_offers(
            self._transport, master_sku, city_id, merchant_uid, page_size
        )

    async def list_products(self, session: KaspiSession, merchant_uid: str) -> list[ListedProduct]:
        """Все товары кабинета в продаже; сессия в фазе `AUTHENTICATED`."""
        return await cabinet.list_products(self._transport, session, merchant_uid)

    async def find_product(
        self, session: KaspiSession, merchant_uid: str, sku: str
    ) -> ListedProduct | None:
        """Свежая строка списка одного товара (`None`, если не найден)."""
        return await cabinet.find_product(self._transport, session, merchant_uid, sku)

    async def get_product(self, session: KaspiSession, merchant_uid: str, sku: str) -> ProductState:
        """Свежее чтение товара."""
        return await cabinet.get_product(self._transport, session, merchant_uid, sku)

    async def set_price(
        self,
        session: KaspiSession,
        product: ProductSnapshot,
        city_prices: Mapping[str, int],
    ) -> PriceWriteReceipt:
        """Одна попытка записи цены; при сбое после отправки - неизвестный исход, без повтора."""
        return await cabinet.set_price(self._transport, session, product, city_prices)

    async def verify_price(
        self,
        session: KaspiSession,
        product: ProductSnapshot,
        expected: Mapping[str, int],
        *,
        settled: bool = False,
    ) -> PriceVerification:
        """Одна проверка по строке списка: совпали ли цены; точки - в `store_anomalies`.

        `settled=True` - точки сверяются при любых ценах (ADR 0037).
        """
        return await cabinet.verify_price(
            self._transport, session, product, expected, settled=settled
        )

    async def login(
        self, credentials: KaspiCredentials, previous: KaspiSession | None = None
    ) -> LoggedIn | CodeRequired:
        """Вход по e-mail и паролю (не проверен на живом Kaspi). Нужен код - `CodeRequired`."""
        return await login.login(self._transport, credentials, previous)

    async def submit_code(self, session: KaspiSession, code: str) -> LoggedIn:
        """Второй шаг входа: код из письма; сессия в фазе `AWAITING_CODE`."""
        return await login.submit_code(self._transport, session, code)

    async def resend_code(self, session: KaspiSession) -> None:
        """Повторная отправка кода из письма; сессия в фазе `AWAITING_CODE`."""
        await login.resend_code(self._transport, session)
