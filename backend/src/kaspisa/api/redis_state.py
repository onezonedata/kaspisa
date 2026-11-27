"""Состояние API в Redis: сессии, счётчики попыток, токены сброса.

Единственное место, где API пишет свои ключи. Токены в ключах не хранятся: только sha256.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid

from redis.asyncio import Redis

from kaspisa.api import deps as api_deps
from kaspisa.api.deps import (
    PASSWORD_CONFIRM_TTL_SECONDS,
    RESEND_INTERVAL_SECONDS,
    RESET_TOKEN_TTL_SECONDS,
    RESTART_MARK_TTL_SECONDS,
    SESSION_ABSOLUTE_TTL_SECONDS,
    SESSION_RENEW_AFTER_SECONDS,
    SESSION_TTL_SECONDS,
    Principal,
)
from kaspisa.db import TenantId

# KEYS[1] - счётчик; ARGV[1] - окно в секундах. Возвращает {число, остаток окна}.
_HIT = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if count == 1 or ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


# KEYS[1] - сессия; ARGV: срок, порог остатка, префикс наборов сессий, now (epoch), абсолютный срок.
# Только EXPIRE: отозванную сессию продление не воскресит. Сессия без created_at или старше
# абсолютного срока удаляется тут же, в том же скрипте. Срок ключа не больше остатка до конца
# абсолютного срока. Возвращает {данные, новый срок ключа в секундах или 0, если не продлена}.
_GET_AND_RENEW = """
local payload = redis.call('GET', KEYS[1])
if not payload then
  return {false, 0}
end
local data = cjson.decode(payload)
local created = data.created_at
local now = tonumber(ARGV[4])
local absolute = tonumber(ARGV[5])
if type(created) ~= 'number' or now - created >= absolute then
  redis.call('DEL', KEYS[1])
  if data.user_id then
    redis.call('SREM', ARGV[3] .. data.user_id, string.sub(KEYS[1], -64))
  end
  return {false, 0}
end
local target = math.min(tonumber(ARGV[1]), created + absolute - now)
local ttl = redis.call('TTL', KEYS[1])
-- продление: остаток больше срока на сутки (порог ARGV[2]) или ключ живёт дольше абсолютного срока
if ttl > target or target - ttl > tonumber(ARGV[1]) - tonumber(ARGV[2]) then
  redis.call('EXPIRE', KEYS[1], target)
  redis.call('EXPIRE', ARGV[3] .. data.user_id, ARGV[1])
  return {payload, target}
end
return {payload, 0}
"""


# KEYS[1] - набор сессий пользователя; ARGV[1] - префикс ключей сессий.
# Чтение набора и удаление - одна атомарная команда: сессия, созданная до или после, не теряется.
_REVOKE_ALL = """
for _, digest in ipairs(redis.call('SMEMBERS', KEYS[1])) do
  redis.call('DEL', ARGV[1] .. digest)
end
redis.call('DEL', KEYS[1])
return 1
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


class RedisState:
    def __init__(self, redis: Redis, *, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix

    def _key(self, *parts: str) -> str:
        return ":".join([self._prefix, *parts])

    @staticmethod
    def _principal(raw: bytes | str | None) -> Principal | None:
        if raw is None:
            return None
        data = json.loads(raw)
        tenant_id = TenantId(uuid.UUID(data["tenant_id"]))
        return Principal(user_id=uuid.UUID(data["user_id"]), tenant_id=tenant_id)

    # --- сессии ---

    async def create_session(self, user_id: uuid.UUID, tenant_id: TenantId) -> str:
        token = secrets.token_urlsafe(32)
        digest = _digest(token)
        payload = json.dumps(
            {
                "user_id": str(user_id),
                "tenant_id": str(tenant_id),
                # начало абсолютного срока жизни сессии (ADR 0012)
                "created_at": int(time.time()),
            }
        )
        members = self._key("user_sess", str(user_id))
        # Одна транзакция Redis: отзыв сессий пользователя видит сессию целиком или не видит.
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(self._key("sess", digest), payload, ex=SESSION_TTL_SECONDS)
            pipe.sadd(members, digest)
            pipe.expire(members, SESSION_TTL_SECONDS)
            await pipe.execute()
        return token

    async def get_session(self, token: str) -> tuple[Principal | None, int | None]:
        """Пользователь сессии и новый срок в секундах, если срок продлён (не чаще раза в сутки).

        Срок ключа - 30 дней, но не дальше 90 дней с входа; набор сессий пользователя продлевается
        до 30 дней. Сессия без `created_at` или старше 90 дней недействительна и удаляется.
        """
        result = await self._redis.eval(
            _GET_AND_RENEW,
            1,
            self._key("sess", _digest(token)),
            str(SESSION_TTL_SECONDS),
            str(SESSION_RENEW_AFTER_SECONDS),
            self._key("user_sess", ""),
            str(int(time.time())),
            str(SESSION_ABSOLUTE_TTL_SECONDS),
        )
        return self._principal(result[0] or None), int(result[1]) or None

    async def delete_session(self, token: str) -> None:
        digest = _digest(token)
        key = self._key("sess", digest)
        principal = self._principal(await self._redis.get(key))
        await self._redis.delete(key)
        if principal is not None:
            await self._redis.srem(self._key("user_sess", str(principal.user_id)), digest)

    async def revoke_sessions_of_token(self, token: str) -> None:
        """Выход на всех устройствах: завершает все сессии владельца токена.

        Владелец определяется той же проверкой, что и вход по cookie: сессия без `created_at`
        или старше абсолютного срока недействительна (удаляется) и ничего не отзывает.
        """
        principal, _ = await self.get_session(token)
        if principal is None:
            return
        await self.delete_session(token)
        await self.revoke_user_sessions(principal.user_id)

    async def revoke_user_sessions(self, user_id: uuid.UUID) -> None:
        members = self._key("user_sess", str(user_id))
        await self._redis.eval(_REVOKE_ALL, 1, members, self._key("sess", ""))
        # Известные устройства забываются вместе с сессиями (ADR 0025, дельта 1).
        await self._redis.delete(self._key("devices", str(user_id)))

    # --- счётчики попыток ---

    async def hit(self, key: str, *, limit: int, window: int) -> int | None:
        """Учесть попытку; `None` - в пределах лимита, иначе остаток окна в секундах."""
        result = await self._redis.eval(_HIT, 1, self._key(key), str(window))
        count, ttl = int(result[0]), int(result[1])
        return None if count <= limit else max(ttl, 1)

    async def clear_login_failures(self, email: str, ip: str) -> None:
        await self._redis.delete(self._key("login_fail", _digest(email), ip))

    async def clear_password_failures(self, user_id: uuid.UUID) -> None:
        await self._redis.delete(self._key("pw_confirm_fail", str(user_id)))

    # --- подтверждение пароля (ADR 0024): окно привязано к токену сессии ---

    async def mark_password_confirmed(self, token: str) -> None:
        await self._redis.set(
            self._key("pw_ok", _digest(token)), "1", ex=PASSWORD_CONFIRM_TTL_SECONDS
        )

    async def password_confirmed(self, token: str) -> bool:
        return bool(await self._redis.exists(self._key("pw_ok", _digest(token))))

    # --- известные устройства (ADR 0025): только sha256 значения cookie ---

    # ZSET: элемент - sha256 cookie, вес - время последнего входа с устройства (секунды Unix).

    async def device_known(self, user_id: uuid.UUID, device: str, *, now: float, ttl: int) -> bool:
        """Устройство известно, если его последний вход моложе `ttl` секунд."""
        score = await self._redis.zscore(self._key("devices", str(user_id)), _digest(device))
        return score is not None and float(score) > now - ttl

    async def remember_device(
        self, user_id: uuid.UUID, device: str, *, now: float, ttl: int, limit: int
    ) -> None:
        """Запоминает вход: просроченные удаляются, остаются `limit` самых свежих устройств."""
        key = self._key("devices", str(user_id))
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zadd(key, {_digest(device): now})
            pipe.zremrangebyscore(key, "-inf", now - ttl)
            pipe.zremrangebyrank(key, 0, -(limit + 1))
            pipe.expire(key, ttl)
            await pipe.execute()

    # --- токены сброса пароля ---

    async def put_reset_token(self, user_id: uuid.UUID, tenant_id: TenantId) -> str:
        token = secrets.token_urlsafe(32)
        payload = json.dumps({"user_id": str(user_id), "tenant_id": str(tenant_id)})
        key = self._key("reset", _digest(token))
        await self._redis.set(key, payload, ex=RESET_TOKEN_TTL_SECONDS)
        return token

    async def take_reset_token(self, token: str) -> Principal | None:
        return self._principal(await self._redis.getdel(self._key("reset", _digest(token))))

    # --- вход в Kaspi: таймер письма и метка «нужен вход заново» ---

    async def resend_left(self, shop_id: uuid.UUID) -> int | None:
        """Остаток таймера повторной отправки кода в секундах; `None` - таймера нет."""
        ttl = int(await self._redis.ttl(self._key("resend", str(shop_id))))
        return ttl if ttl > 0 else None

    async def start_resend_timer(self, shop_id: uuid.UUID, *, only_if_free: bool) -> int | None:
        """Ставит таймер письма с кодом.

        `only_if_free=True`: `None` - таймер поставлен, иначе остаток в секундах.
        `only_if_free=False`: новое письмо ушло при входе, таймер ставится всегда.
        """
        key = self._key("resend", str(shop_id))
        if not only_if_free:
            await self._redis.set(key, "1", ex=RESEND_INTERVAL_SECONDS)
            return None
        if await self._redis.set(key, "1", nx=True, ex=RESEND_INTERVAL_SECONDS):
            return None
        return max(int(await self._redis.ttl(key)), 1)

    async def clear_resend_timer(self, shop_id: uuid.UUID) -> None:
        """Письмо заведомо не ушло: таймер снимается."""
        await self._redis.delete(self._key("resend", str(shop_id)))

    # --- «Узнать цены» (ADR 0030): таймер повтора по товару ---

    async def start_price_read_timer(self, product_id: int) -> int | None:
        """`SET NX EX`: `None` - таймер поставлен, иначе остаток срока в секундах (не меньше 1)."""
        key = self._key("price_read", str(product_id))
        if await self._redis.set(key, "1", nx=True, ex=api_deps.PRICE_READ_REPEAT_SECONDS):
            return None
        return max(int(await self._redis.ttl(key)), 1)

    async def price_read_left(self, product_id: int) -> int:
        """Остаток таймера повтора в секундах, не меньше 1 (для `retry_after_seconds`)."""
        return max(int(await self._redis.ttl(self._key("price_read", str(product_id)))), 1)

    async def clear_price_read_timer(self, product_id: int) -> None:
        """Запрос в Kaspi заведомо не ушёл: повтор разрешён сразу."""
        await self._redis.delete(self._key("price_read", str(product_id)))

    async def mark_restart(self, shop_id: uuid.UUID, version: int) -> None:
        await self._redis.set(
            self._key("restart", str(shop_id)), str(version), ex=RESTART_MARK_TTL_SECONDS
        )

    async def restart_marked(self, shop_id: uuid.UUID, version: int) -> bool:
        raw = await self._redis.get(self._key("restart", str(shop_id)))
        return raw is not None and _text(raw) == str(version)
