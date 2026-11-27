"""Главный цикл обработчика: захват магазинов, слоты, остановка по событию и сигналу."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
from datetime import datetime, timedelta

from redis.asyncio import Redis
from redis.exceptions import RedisError

from kaspisa.config import Settings
from kaspisa.db import ClaimedShop, create_database
from kaspisa.kaspi import KaspiClient
from kaspisa.worker.constants import (
    IDLE_SLEEP_SECONDS,
    LEASE_SECONDS,
    LIMITER_MAX_WAIT_SECONDS,
    MIN_RECHECK_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    UNBOUNDED_RECHECK_SECONDS,
    WORKER_SLOTS,
)
from kaspisa.worker.context import WorkerConfig, WorkerContext
from kaspisa.worker.redis_limits import RedisIntervalLimiter, publish_dry_run
from kaspisa.worker.slice import run_shop_slice

logger = logging.getLogger(__name__)

# Как часто обработчик обновляет свой флаг `dry_run` в Redis (срок жизни ключа - 120 с).
DRY_RUN_PUBLISH_INTERVAL = timedelta(seconds=30)

# Префикс ключей Redis обработчика: общий у всех процессов, чтобы лимиты были общими.
REDIS_PREFIX = "kaspisa"


class Worker:
    """Захватывает магазины и запускает слайсы, пока не поднято событие остановки."""

    def __init__(self, ctx: WorkerContext, *, idle_sleep: float = IDLE_SLEEP_SECONDS) -> None:
        self._ctx = ctx
        self._idle_sleep = idle_sleep
        self._published_at: datetime | None = None

    async def _publish_dry_run(self) -> None:
        """Сообщает API флаг `dry_run` при старте и затем не реже раза в 30 секунд (ADR 0024).

        Сбой Redis пишется в лог и цикл не останавливает: повтор через интервал.
        """
        now = self._ctx.clock()
        if self._published_at is not None and now - self._published_at < DRY_RUN_PUBLISH_INTERVAL:
            return
        self._published_at = now
        try:
            await publish_dry_run(
                self._ctx.redis, self._ctx.limiter.prefix, self._ctx.config.dry_run
            )
        except (RedisError, OSError) as exc:
            logger.error("флаг dry_run не опубликован: %s", type(exc).__name__)

    async def _claim(self) -> ClaimedShop | None:
        ctx = self._ctx
        try:
            async with ctx.database.system() as system:
                return await system.claim_shop(
                    worker_id=ctx.config.worker_id,
                    now=ctx.clock(),
                    lease_seconds=LEASE_SECONDS,
                    recheck_seconds=MIN_RECHECK_SECONDS,
                    unbounded_recheck_seconds=UNBOUNDED_RECHECK_SECONDS,
                )
        except Exception as exc:
            logger.error("захват магазина не удался: %s", type(exc).__name__)
            return None

    async def _slice(self, claim: ClaimedShop, stop: asyncio.Event) -> None:
        try:
            await run_shop_slice(self._ctx, claim, stop)
        except Exception as exc:  # слайс не должен ронять процесс; без текста: он может быть чужим
            logger.error("слайс магазина %s: %s", claim.shop_id, type(exc).__name__)

    async def run(self, stop: asyncio.Event) -> None:
        tasks: set[asyncio.Task[None]] = set()
        while not stop.is_set():
            await self._publish_dry_run()
            claim = await self._claim() if len(tasks) < self._ctx.config.slots else None
            if stop.is_set():
                break  # захваченный в момент остановки магазин вернёт по сроку аренды
            if claim is not None:
                task = asyncio.create_task(self._slice(claim, stop))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                await asyncio.sleep(0)
                continue
            if tasks:
                await asyncio.wait(tasks, timeout=self._idle_sleep)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._idle_sleep)
        await self._drain(tasks)

    async def _drain(self, tasks: set[asyncio.Task[None]]) -> None:
        """Ждёт слайсы не дольше `SHUTDOWN_GRACE_SECONDS`, остальные отменяет и дожидается."""
        if not tasks:
            return
        pending = set(tasks)
        _, pending = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        if pending:  # отмена доводит слайс до `finally`: запрет сохранён, аренда снята
            await asyncio.gather(*pending, return_exceptions=True)


def worker_config(settings: Settings) -> WorkerConfig:
    """Настройки процесса: режим «без записи» из окружения (по умолчанию включён)."""
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
    return WorkerConfig(worker_id=worker_id, dry_run=settings.dry_run, slots=WORKER_SLOTS)


async def run_worker(settings: Settings) -> None:
    """Запускает обработчик и останавливает его по SIGTERM или SIGINT."""
    config = worker_config(settings)
    database = create_database(settings.database_url)
    redis: Redis = Redis.from_url(settings.redis_url)
    limiter = RedisIntervalLimiter(redis, prefix=REDIS_PREFIX, max_wait=LIMITER_MAX_WAIT_SECONDS)
    client = KaspiClient(limiter=limiter)
    ctx = WorkerContext(
        database=database, client=client, redis=redis, limiter=limiter, config=config
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled = (signal.SIGTERM, signal.SIGINT)
    for sig in handled:
        loop.add_signal_handler(sig, stop.set)
    logger.info("обработчик %s запущен, dry_run=%s", config.worker_id, config.dry_run)
    try:
        await Worker(ctx).run(stop)
    finally:
        for sig in handled:
            loop.remove_signal_handler(sig)
        await client.aclose()
        await redis.aclose()
        await database.dispose()
        logger.info("обработчик %s остановлен", config.worker_id)
