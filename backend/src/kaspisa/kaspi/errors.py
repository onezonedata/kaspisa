"""Исключения клиента Kaspi.

Тексты и атрибуты содержат только имя операции, статус, код Kaspi, пути полей и `sku`.
Тел ответов, заголовков, адресов со строкой запроса, cookie, логина, пароля и кода в них нет.
"""

from __future__ import annotations

from enum import StrEnum


class KaspiError(Exception):
    """Базовое исключение клиента Kaspi."""


class KaspiTransportError(KaspiError):
    """Запрос не дал ответа: сбой сети, чтения тела или адреса."""

    def __init__(self, operation: str, kind: str) -> None:
        super().__init__(f"{operation}: запрос не дал ответа ({kind})")
        self.operation = operation
        self.kind = kind


class UnexpectedStatusError(KaspiError):
    """Статус ответа вне 2xx, не распознанный иначе."""

    def __init__(self, operation: str, status_code: int) -> None:
        super().__init__(f"{operation}: неожиданный статус {status_code}")
        self.operation = operation
        self.status_code = status_code


class ResponseFormatError(KaspiError):
    """Формат ответа изменился: `fields` - пути полей без значений."""

    def __init__(self, operation: str, fields: tuple[str, ...] = ()) -> None:
        detail = ", ".join(fields) if fields else "формат ответа не распознан"
        super().__init__(f"{operation}: изменился формат ответа ({detail})")
        self.operation = operation
        self.fields = fields


class RateLimitedError(KaspiError):
    """Отказ по лимиту (429)."""

    def __init__(self, operation: str, retry_after: int | None = None) -> None:
        super().__init__(f"{operation}: отказ по лимиту запросов")
        self.operation = operation
        self.retry_after = retry_after


class SessionExpiredError(KaspiError):
    """Сессия кабинета не принята."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"{operation}: сессия кабинета не принята")
        self.operation = operation


class LoginRejectedError(KaspiError):
    """Kaspi отклонил вход."""

    def __init__(self, code: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(f"вход отклонён: {code}")
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class WriteRefusal(StrEnum):
    """Причина, по которой клиент отказался записывать цену."""

    # Клиент не поднимает с ADR 0037; оставлено для старых строк журнала и стабильной схемы API.
    STOCK_COUNTED = "stock_counted"
    UNVERIFIED_STORE_STATE = "unverified_store_state"
    PRICE_MISMATCH = "price_mismatch"
    PREORDER = "preorder"
    ARCHIVED = "archived"
    NO_STORES = "no_stores"
    STORES_CHANGED = "stores_changed"
    STALE_LISTING = "stale_listing"
    # Клиент не поднимает с ADR 0019; оставлено для старых строк журнала и стабильной схемы API.
    MODEL_MISSING = "model_missing"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    INVALID_PRICES = "invalid_prices"


class WriteRefusedError(KaspiError):
    """Клиент отказался записывать цену; запрос не отправлен."""

    def __init__(self, reason: WriteRefusal, sku: str) -> None:
        super().__init__(f"запись цены {sku} отклонена клиентом: {reason.value}")
        self.reason = reason
        self.sku = sku


class PriceWriteOutcomeUnknownError(KaspiError):
    """Запись отправлена, итог неизвестен; повторять вслепую нельзя."""

    def __init__(self, sku: str, kind: str) -> None:
        super().__init__(f"запись цены {sku}: итог неизвестен ({kind})")
        self.sku = sku
        self.kind = kind


class SessionStateError(KaspiError):
    """Метод вызван не в той фазе сессии или операция недопустима из состояния подключения."""


class SessionBlobError(KaspiError):
    """Сохранённый блок сессии не разобран."""
