"""Обёртка открытого значения секрета."""

from __future__ import annotations

import hmac
from typing import NoReturn


class Secret:
    """Открытое значение, которое нельзя случайно вывести или сериализовать.

    Получить значение можно только через `reveal()`.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "Secret('***')"

    def __format__(self, format_spec: str) -> str:
        return format(repr(self), format_spec)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode(), other._value.encode())

    __hash__ = None  # type: ignore[assignment]

    def __reduce__(self) -> NoReturn:
        raise TypeError("секрет нельзя сериализовать или копировать")
