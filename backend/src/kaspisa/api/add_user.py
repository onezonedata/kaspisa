"""Команда оператора: завести сотрудника в учётную запись владельца (ADR 0027).

Запуск из backend/: `uv run python -m kaspisa.api.add_user OWNER_EMAIL EMAIL`. Пользователь
создаётся в учётной записи, где состоит `OWNER_EMAIL`, с непригодным паролем; печатается токен
сброса пароля (один раз, в stdout), сотрудник задаёт пароль сам. Коды выхода: 0 - добавлен,
1 - владельца нет или e-mail занят, 2 - ошибка запуска или аргументов.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from collections.abc import Sequence

from pydantic import ValidationError
from redis.asyncio import Redis

from kaspisa.api.passwords import hash_password
from kaspisa.api.redis_state import RedisState
from kaspisa.api.reset_token import REDIS_API_PREFIX
from kaspisa.api.schemas import LoginIn
from kaspisa.config import Settings
from kaspisa.db import EmailTakenError, TenantId, create_database

USAGE = "использование: python -m kaspisa.api.add_user OWNER_EMAIL EMAIL"


class _OwnerNotFoundError(Exception):
    """Пользователя `OWNER_EMAIL` нет."""


class _TokenNotIssuedError(Exception):
    """Сотрудник создан, но токен сброса не записан (сбой Redis); аргумент - имя исключения."""


def _clean_email(value: str) -> str | None:
    """Адрес в виде, как при регистрации (формат `RegisterIn`), или `None`."""
    try:
        return LoginIn(email=value, password="x").email  # noqa: S106 - пароль не используется
    except ValidationError:
        return None


async def _add(owner_email: str, email: str, settings: Settings) -> tuple[str, int]:
    """Создаёт пользователя; возвращает токен сброса и число пользователей учётной записи."""
    database = create_database(settings.database_url)
    redis: Redis = Redis.from_url(settings.redis_url)
    try:
        async with database.system() as system:
            owner = await system.find_user_by_email(owner_email)
        if owner is None:
            raise _OwnerNotFoundError
        tenant_id = TenantId(owner.tenant_id)
        # Пароль - хеш случайной строки, которую никто не видит: войти с ним нельзя.
        unusable = hash_password(secrets.token_urlsafe(32))
        async with database.tenant(tenant_id) as scope:
            user = await scope.users.add(email=email, password_hash=unusable)
            await scope.commit()
            count = len(await scope.users.list())
        state = RedisState(redis, prefix=REDIS_API_PREFIX)
        try:
            return await state.put_reset_token(user.id, tenant_id), count
        except Exception as exc:
            # Сотрудник уже записан в базу: отличаем это от сбоя до создания.
            raise _TokenNotIssuedError(type(exc).__name__) from None
    finally:
        await redis.aclose()
        await database.dispose()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    owner_email = _clean_email(args[0])
    email = _clean_email(args[1])
    if owner_email is None or email is None:
        print(f"неверный формат e-mail; {USAGE}", file=sys.stderr)
        return 2
    try:
        token, count = asyncio.run(_add(owner_email, email, settings or Settings()))
    except _OwnerNotFoundError:
        print(f"пользователь {owner_email} не найден", file=sys.stderr)
        return 1
    except EmailTakenError:
        print("e-mail занят", file=sys.stderr)
        return 1
    except _TokenNotIssuedError as exc:
        print(
            f"{email} создан в учётной записи {owner_email}, но токен не выдан "
            f"(ошибка Redis: {exc}). Получите токен командой "
            f"`python -m kaspisa.api.reset_token {email}` после восстановления Redis.",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        # Текст исключения драйвера может содержать адрес или пароль базы.
        print(f"ошибка запуска: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(
        f"{email} добавлен в учётную запись {owner_email} (пользователей в ней: {count}). "
        "Токен ниже действует 1 час и даёт вход в эту учётную запись. "
        "Не передавайте его помощнику-агенту и в чат.",
        file=sys.stderr,
    )
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
