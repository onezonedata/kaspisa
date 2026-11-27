"""Ограничитель частоты запросов к Kaspi (в памяти одного процесса)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class IntervalRateLimiter:
    """Пускает запросы одного ключа не чаще раза в `min_interval` секунд.

    Очередь по ключу честная; ожидание разных ключей друг друга не блокирует.
    Часы и `sleep` подменяются в тестах, но сам ограничитель отключить нельзя.
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        """Ждёт своей очереди по ключу."""
        async with self._lock:
            now = self._clock()
            start = max(now, self._next_at.get(key, now))
            self._next_at[key] = start + self._min_interval
        delay = start - now
        if delay > 0:
            await self._sleep(delay)
