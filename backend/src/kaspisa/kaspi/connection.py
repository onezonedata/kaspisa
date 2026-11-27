"""Состояние подключения магазина: допустимость операций и переходы (ADR 0003, ADR 0005).

Чистые функции без сети и базы. Строки состояний совпадают со значениями
`ShopConnectionState` (этап 4); пакет `kaspisa.db` не импортируется, совпадение проверяет тест.
Вызывающий код (этапы 5 и 6) перед запросом к Kaspi зовёт `ensure_operation_allowed`, после -
`next_connection_state` и записывает результат в базу с `expected_version`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kaspisa.kaspi.errors import LoginRejectedError, SessionExpiredError, SessionStateError
from kaspisa.kaspi.models import CodeRequired, LoggedIn

ConnectionState = Literal["not_connected", "awaiting_code", "connected", "reauth_required"]
ConnectionOperation = Literal["login", "login_restart", "submit_code", "resend_code", "cabinet"]

# Временные отказы входа (предположение П16): состояние не меняется, повтор - решает вызывающий.
TEMPORARY_LOGIN_CODES = frozenset({"TIMEOUT_EXCEEDED", "MFA_SEND_FLOOD", "MFA_CODE_TOO_MANY_SEND"})

_ALLOWED: dict[str, frozenset[str]] = {
    "not_connected": frozenset({"login"}),
    "awaiting_code": frozenset({"login_restart", "submit_code", "resend_code"}),
    "connected": frozenset({"cabinet"}),
    "reauth_required": frozenset({"login"}),
}
_LOGIN_OPERATIONS = frozenset({"login", "login_restart", "submit_code", "resend_code"})


@dataclass(frozen=True)
class ConnectionChange:
    """Новое состояние; `error` не `None` только у `reauth_required`."""

    state: ConnectionState
    error: str | None


def ensure_operation_allowed(current: ConnectionState, operation: ConnectionOperation) -> None:
    """Проверяет операцию ДО запроса к Kaspi (после запроса письмо с кодом уже ушло)."""
    if operation not in _ALLOWED.get(current, frozenset()):
        raise SessionStateError(f"операция {operation} недопустима из состояния {current}")


def _check_possible(operation: ConnectionOperation, outcome: object) -> None:
    """Правило 9: исход, который эта операция дать не может."""
    possible = True
    if isinstance(outcome, LoggedIn):
        possible = operation in ("login", "login_restart", "submit_code")
    elif isinstance(outcome, CodeRequired):
        possible = operation in ("login", "login_restart")
    elif isinstance(outcome, SessionExpiredError):
        possible = operation == "cabinet"
    elif isinstance(outcome, LoginRejectedError):
        possible = operation in _LOGIN_OPERATIONS
    if not possible:
        raise SessionStateError(f"исход {type(outcome).__name__} невозможен для {operation}")


def next_connection_state(
    current: ConnectionState, operation: ConnectionOperation, outcome: object
) -> ConnectionChange | None:
    """Новое состояние по исходу операции; `None` - состояние не менять.

    `outcome` - результат метода клиента либо пойманное исключение `KaspiError`.
    Порядок: допустимость операции (правило 1), невозможный исход (9), затем правила 2-7;
    остальное (правило 8) - `None`.
    """
    ensure_operation_allowed(current, operation)
    _check_possible(operation, outcome)
    if isinstance(outcome, LoggedIn):
        return ConnectionChange("connected", None)
    if isinstance(outcome, CodeRequired):
        return ConnectionChange("awaiting_code", None)
    if isinstance(outcome, LoginRejectedError):
        if outcome.code in TEMPORARY_LOGIN_CODES:
            return None
        if operation == "submit_code" and outcome.code == "MFA_CODE_INVALID":
            return None
        return ConnectionChange("reauth_required", outcome.code)
    if isinstance(outcome, SessionExpiredError):
        return ConnectionChange("reauth_required", "session_expired")
    return None
