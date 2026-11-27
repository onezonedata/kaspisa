"""Состояние загрузки каталога для API: идёт ли она сейчас (ADR 0017, П5)."""

from __future__ import annotations

import uuid

from kaspisa.worker.redis_limits import SyncLock


class ApiSyncLock(SyncLock):
    """`SyncLock` с чтением состояния: занята ли блокировка загрузки каталога магазина.

    Ключ берётся из унаследованного `_key`, поэтому не расходится с `acquire` обработчика.
    Блокировка здесь не занимается: обработчик увидел бы её занятой.
    """

    async def held(self, shop_id: uuid.UUID) -> bool:
        return bool(await self._redis.exists(self._key(shop_id)))
