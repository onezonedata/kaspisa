"""Команда оператора: выдать токен сброса пароля (почтового сервиса нет).

Запуск из backend/: `uv run python -m kaspisa.api.reset_token EMAIL`. Токен печатается
один раз в stdout. Коды выхода: 0 - выдан, 1 - пользователя нет, 2 - ошибка запуска.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from redis.asyncio import Redis

from kaspisa.api.deps import ApiDeps
from kaspisa.api.redis_state import RedisState
from kaspisa.config import Settings
from kaspisa.db import TenantId, create_database

REDIS_API_PREFIX = "kaspisa:api"


async def issue_reset_token(deps: ApiDeps, email: str) -> str | None:
    """Токен для пользователя или `None`, если такого e-mail нет."""
    async with deps.database.system() as system:
        user = await system.find_user_by_email(email.strip().lower())
    if user is None:
        return None
    return await deps.state.put_reset_token(user.id, TenantId(user.tenant_id))


async def _issue(email: str, settings: Settings) -> str | None:
    database = create_database(settings.database_url)
    redis: Redis = Redis.from_url(settings.redis_url)
    try:
        state = RedisState(redis, prefix=REDIS_API_PREFIX)
        async with database.system() as system:
            user = await system.find_user_by_email(email.strip().lower())
        if user is None:
            return None
        return await state.put_reset_token(user.id, TenantId(user.tenant_id))
    finally:
        await redis.aclose()
        await database.dispose()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("использование: python -m kaspisa.api.reset_token EMAIL", file=sys.stderr)
        return 2
    try:
        token = asyncio.run(_issue(args[0], settings or Settings()))
    except Exception as exc:
        # Текст исключения драйвера может содержать адрес или пароль базы.
        print(f"ошибка запуска: {type(exc).__name__}", file=sys.stderr)
        return 2
    if token is None:
        print("пользователь не найден", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
