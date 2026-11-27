"""Ошибки слоя данных. Тексты содержат только имя ограничения, без значений строк."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


class NotFoundError(Exception):
    """Строки нет или она принадлежит другому арендатору."""


class StaleStateError(Exception):
    """connection_version не совпала: запись сделал устаревший писатель."""


class ConstraintViolationError(Exception):
    """Нарушено ограничение базы. Значений строки в ошибке нет."""

    def __init__(self, constraint: str | None) -> None:
        super().__init__(constraint or "ограничение базы нарушено")
        self.constraint = constraint


class EmailTakenError(ConstraintViolationError):
    """E-mail уже занят."""


class ShopAlreadyConnectedError(ConstraintViolationError):
    """Магазин Kaspi уже подключён."""


_EMAIL_CONSTRAINT = "uq_app_user_email"
_MERCHANT_CONSTRAINT = "uq_shop_merchant_uid"


def _constraint_name(exc: IntegrityError) -> str | None:
    """Имя ограничения из структурных полей исключения драйвера, а не из текста."""
    cause = exc.orig.__cause__ if exc.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name if isinstance(name, str) else None


def _error_for(name: str | None) -> ConstraintViolationError:
    if name == _EMAIL_CONSTRAINT:
        return EmailTakenError(name)
    if name == _MERCHANT_CONSTRAINT:
        return ShopAlreadyConnectedError(name)
    return ConstraintViolationError(name)


async def run_guarded[T](session: AsyncSession, operation: Callable[[], Awaitable[T]]) -> T:
    """Выполнить flush, commit или запрос; нарушение ограничения станет ошибкой пакета.

    Ошибка бросается вне блока except: так в `__cause__` и `__context__` не остаётся
    исключения драйвера, а в нём DETAIL Postgres со значениями строки.
    """
    name: str | None = None
    violated = False
    try:
        return await operation()
    except IntegrityError as exc:
        violated = True
        name = _constraint_name(exc)
        await session.rollback()
    if violated:
        raise _error_for(name)
    raise AssertionError  # недостижимо
