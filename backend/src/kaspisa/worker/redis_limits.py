"""Redis: общий ограничитель частоты запросов к Kaspi и блокировка «один вход на магазин».

Время ограничителя в бою берёт сам Redis (`TIME`): часы хостов процессов не важны.
Внедряемые часы нужны только тестам. Недоступный Redis ограничитель не обходит:
`acquire` бросает `LimiterUnavailableError`, запрос к Kaspi не отправляется.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import RedisError

from kaspisa.kaspi import IntervalRateLimiter

# KEYS[1] — ключ; ARGV[1] — интервал в мс; ARGV[2] — «сейчас» в мс или '' (время Redis);
# ARGV[3] — предел ожидания в мс или '' (без предела).
# Возвращает ожидание в мс; если оно больше предела, слот не занимается и возвращается
# -(ожидание) - 1. Запись и чтение времени в одном скрипте: атомарно.
_ACQUIRE = """
local now
if ARGV[2] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
else
  now = tonumber(ARGV[2])
end
local interval = tonumber(ARGV[1])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local start = math.max(now, nxt)
if ARGV[3] ~= '' and start - now > tonumber(ARGV[3]) then
  return -(start - now) - 1
end
redis.call('SET', KEYS[1], tostring(start + interval), 'PX', start + interval - now + 60000)
return start - now
"""

# KEYS[1] — ключ; ARGV[1] — не используется; ARGV[2] — «сейчас» в мс или ''.
# Возвращает, сколько мс ещё ждать до пропуска по ключу (без изменений в Redis).
_PENDING = """
local now
if ARGV[2] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
else
  now = tonumber(ARGV[2])
end
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
return math.max(0, nxt - now)
"""

# KEYS[1] — ключ; ARGV[1] — пауза в мс; ARGV[2] — «сейчас» в мс или ''.
_PENALIZE = """
local now
if ARGV[2] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
else
  now = tonumber(ARGV[2])
end
local pause = tonumber(ARGV[1])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local target = math.max(nxt, now + pause)
redis.call('SET', KEYS[1], tostring(target), 'PX', target - now + 60000)
return target - now
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class LimiterUnavailableError(Exception):
    """Redis недоступен: ограничитель не может пропустить запрос."""


class LimiterBusyError(Exception):
    """Ожидание ограничителя дольше предела: слот не занят, запрос не отправлен.

    `wait_seconds` - сколько ещё ждать пропуска по ключу.
    """

    def __init__(self, wait_seconds: float) -> None:
        super().__init__("ограничитель занят")
        self.wait_seconds = wait_seconds


class RedisIntervalLimiter(IntervalRateLimiter):
    """Ограничитель частоты на несколько процессов: `next_at` по ключу хранится в Redis."""

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        min_interval: float = 1.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait: float | None = None,
    ) -> None:
        super().__init__(min_interval, sleep=sleep)
        self._max_wait = max_wait  # None - ждать сколько нужно
        self._redis = redis
        self._prefix = prefix
        self._time = clock
        self.failures = 0  # число сбоев Redis в acquire и penalize

    @property
    def prefix(self) -> str:
        """Префикс ключей Redis: от него строится и блокировка входа."""
        return self._prefix

    def _now_arg(self) -> str:
        return "" if self._time is None else str(int(self._time() * 1000))

    def _key(self, key: str) -> str:
        return f"{self._prefix}:rl:{key}"

    async def _run(self, script: str, key: str, millis: int, *extra: str) -> int:
        try:
            result = await self._redis.eval(
                script, 1, self._key(key), str(millis), self._now_arg(), *extra
            )
        except (RedisError, OSError) as exc:
            self.failures += 1
            raise LimiterUnavailableError(type(exc).__name__) from None
        return int(result)

    async def acquire(self, key: str) -> None:
        limit = "" if self._max_wait is None else str(int(self._max_wait * 1000))
        delay_ms = await self._run(_ACQUIRE, key, int(self._min_interval * 1000), limit)
        if delay_ms < 0:
            raise LimiterBusyError((-delay_ms - 1) / 1000)
        if delay_ms > 0:
            await self._sleep(delay_ms / 1000)

    async def pending_delay(self, key: str) -> float:
        """Сколько секунд пришлось бы ждать пропуска по ключу сейчас; слот не занимается."""
        return await self._run(_PENDING, key, 0) / 1000

    async def penalize(self, key: str, seconds: float) -> None:
        """Сдвигает следующий пропуск ключа на `seconds` (после 429); не сокращает ожидание."""
        await self._run(_PENALIZE, key, int(seconds * 1000))


class LoginLock:
    """Блокировка «один вход на магазин»: `SET NX PX`, снятие только по своему токену."""

    _name = "login"

    def __init__(self, redis: Redis, *, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, shop_id: uuid.UUID) -> str:
        return f"{self._prefix}:{self._name}:{shop_id}"

    async def acquire(self, shop_id: uuid.UUID, ttl_ms: int) -> str | None:
        token = secrets.token_hex(16)
        taken = await self._redis.set(self._key(shop_id), token, nx=True, px=ttl_ms)
        return token if taken else None

    async def release(self, shop_id: uuid.UUID, token: str) -> bool:
        result = await self._redis.eval(_RELEASE, 1, self._key(shop_id), token)
        return bool(result)

    @asynccontextmanager
    async def hold(self, shop_id: uuid.UUID, ttl_ms: int) -> AsyncIterator[bool]:
        """`True`, если блокировка получена; снимается на выходе."""
        token = await self.acquire(shop_id, ttl_ms)
        try:
            yield token is not None
        finally:
            if token is not None:
                await self.release(shop_id, token)


class SyncLock(LoginLock):
    """Блокировка «одна загрузка каталога на магазин»: общая у кнопки API и обработчика."""

    _name = "sync"


class PriceReadLock(LoginLock):
    """Блокировка «одно чтение «Узнать цены» на магазин» (ADR 0030)."""

    _name = "price_read"


# Срок жизни флага обработчика: не обновлён за 2 минуты - обработчик не отвечает (ADR 0024).
WORKER_DRY_RUN_TTL_SECONDS = 120


def _dry_run_key(prefix: str) -> str:
    return f"{prefix}:worker:dry_run"


async def publish_dry_run(redis: Redis, prefix: str, dry_run: bool) -> None:
    """Обработчик сообщает API своё `KASPISA_DRY_RUN`: `"1"` - запись запрещена, `"0"` - нет."""
    await redis.set(_dry_run_key(prefix), "1" if dry_run else "0", ex=WORKER_DRY_RUN_TTL_SECONDS)


async def read_dry_run(redis: Redis, prefix: str) -> bool | None:
    """Флаг обработчика; `None` - ключа нет (обработчик не отзывался 2 минуты)."""
    raw = await redis.get(_dry_run_key(prefix))
    if raw is None:
        return None
    return (raw.decode() if isinstance(raw, bytes) else str(raw)) == "1"
