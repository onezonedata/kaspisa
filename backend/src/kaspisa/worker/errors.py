"""Внутренние исключения обработчика: прерывают слайс магазина, процесс не роняют."""

from __future__ import annotations

from datetime import datetime


class ShopUnavailable(Exception):
    """Работа с магазином в этом слайсе невозможна.

    `until` - срок пропуска магазина (`defer` аренды); `None` - аренда просто снимается.
    """

    def __init__(self, until: datetime | None = None) -> None:
        super().__init__("магазин недоступен в этом слайсе")
        self.until = until


class ShopSkipped(ShopUnavailable):
    """Магазин пропущен до начала работы (состояние, ключи, пауза входа)."""


class ShopThrottled(Exception):
    """Kaspi ответил 429: пауза поставлена в ограничитель, слайс магазина заканчивается.

    `until` - конец паузы: магазин откладывается до него (`defer`).
    """

    def __init__(self, until: datetime) -> None:
        super().__init__("Kaspi ограничил частоту запросов")
        self.until = until
