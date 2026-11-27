"""Числа обработчика (ADR 0008, раздел 6 проекта этапа)."""

from kaspisa.db import CATALOG_REFRESH_SECONDS
from kaspisa.kaspi import (
    PRICE_VERIFY_DEADLINE_SECONDS,
    PRICE_VERIFY_FIRST_CHECK_SECONDS,
    PRICE_WRITE_HOLD_SECONDS,
)

MIN_RECHECK_SECONDS = 60
# Перепроверка товара без границ (ADR 0030): только чтение цен, реже, чем товара с границами.
UNBOUNDED_RECHECK_SECONDS = 600
# Аренда магазина. Продлевается перед каждым обращением к Kaspi и после каждого шага, поэтому
# между продлениями проходит один запрос: до LIMITER_MAX_WAIT_SECONDS ожидания ограничителя и
# повторы транспорта чтения (3 попытки по 15 с с паузами) или SET_PRICE_TIMEOUT_SECONDS.
# Инвариант: LEASE_SECONDS > LIMITER_MAX_WAIT_SECONDS + 3 * 15 + паузы повторов (~30 с) +
# запас на запись (SET_PRICE_TIMEOUT_SECONDS). Слайс (SLICE_MAX_SECONDS) в него не входит.
LEASE_SECONDS = 240
MAX_VERIFY_PER_SLICE = 20
SLICE_MAX_PRODUCTS = 20
SLICE_MAX_SECONDS = 30
WORKER_SLOTS = 3
IDLE_SLEEP_SECONDS = 5
SHUTDOWN_GRACE_SECONDS = 30
SET_PRICE_TIMEOUT_SECONDS = 45
PRICE_WRITE_LIMIT = 250
PRICE_WRITE_WINDOW_SECONDS = 1800
PERMANENT_REFUSAL_RETRY_SECONDS = 1800
VERIFY_AFTER_DEADLINE_INTERVAL_SECONDS = 300
LOGIN_ERROR_LIMIT = 3
WRITE_ERROR_LIMIT = 3
# Срок пропуска магазина (`defer`) при недоступном Redis и временном отказе входа без срока.
LOGIN_BACKOFF_DEFAULT_SECONDS = 300
RATE_LIMIT_PAUSE_MIN_SECONDS = 60
UNKNOWN_KEY_SKIP_SECONDS = 600
# Пауза магазина после непредвиденной ошибки слайса (без горячего цикла).
UNEXPECTED_ERROR_PAUSE_SECONDS = 60
# Ожидание ограничителя в слайсе: дольше - `LimiterBusyError` и пропуск магазина.
LIMITER_MAX_WAIT_SECONDS = 30
# Запись не начинается, если ожидание ограничителя `mc:<uid>` дольше этого.
WRITE_MAX_LIMITER_WAIT_SECONDS = 3
# Блокировка «один вход на магазин»: срок жизни и пауза, если её держит другой процесс.
LOGIN_LOCK_TTL_SECONDS = 60
LOGIN_LOCK_BUSY_SECONDS = 30

# Загрузка каталога обработчиком (ADR 0010, Н7). Срок между загрузками - CATALOG_REFRESH_SECONDS
# (задан в базе: от него зависит захват магазина). Пауза после сбоя: на каждом захвате
# магазин не пробуют. Предел вызова меньше LEASE_SECONDS и срока блокировки.
CATALOG_RETRY_SECONDS = 3600
CATALOG_CALL_TIMEOUT_SECONDS = 200
CATALOG_LOCK_TTL_SECONDS = 300
# Блокировку держит кнопка API или другой обработчик: повторная попытка не раньше.
CATALOG_LOCK_BUSY_SECONDS = 300

__all__ = [
    "CATALOG_CALL_TIMEOUT_SECONDS",
    "CATALOG_LOCK_BUSY_SECONDS",
    "CATALOG_LOCK_TTL_SECONDS",
    "CATALOG_REFRESH_SECONDS",
    "CATALOG_RETRY_SECONDS",
    "IDLE_SLEEP_SECONDS",
    "LEASE_SECONDS",
    "LIMITER_MAX_WAIT_SECONDS",
    "LOGIN_BACKOFF_DEFAULT_SECONDS",
    "LOGIN_ERROR_LIMIT",
    "LOGIN_LOCK_BUSY_SECONDS",
    "LOGIN_LOCK_TTL_SECONDS",
    "MAX_VERIFY_PER_SLICE",
    "MIN_RECHECK_SECONDS",
    "PERMANENT_REFUSAL_RETRY_SECONDS",
    "PRICE_VERIFY_DEADLINE_SECONDS",
    "PRICE_VERIFY_FIRST_CHECK_SECONDS",
    "PRICE_WRITE_HOLD_SECONDS",
    "PRICE_WRITE_LIMIT",
    "PRICE_WRITE_WINDOW_SECONDS",
    "RATE_LIMIT_PAUSE_MIN_SECONDS",
    "SET_PRICE_TIMEOUT_SECONDS",
    "SHUTDOWN_GRACE_SECONDS",
    "SLICE_MAX_PRODUCTS",
    "SLICE_MAX_SECONDS",
    "UNBOUNDED_RECHECK_SECONDS",
    "UNEXPECTED_ERROR_PAUSE_SECONDS",
    "UNKNOWN_KEY_SKIP_SECONDS",
    "VERIFY_AFTER_DEADLINE_INTERVAL_SECONDS",
    "WORKER_SLOTS",
    "WRITE_ERROR_LIMIT",
    "WRITE_MAX_LIMITER_WAIT_SECONDS",
]
