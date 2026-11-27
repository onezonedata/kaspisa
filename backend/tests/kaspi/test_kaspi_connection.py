"""Состояние подключения: допустимость операций и переходы (Т65, Т66; ADR 0003, ADR 0005)."""

from __future__ import annotations

import itertools
import typing
from typing import Any, get_args

import pytest
from kaspi_testing import authenticated_session, load_fixture, session_blob

from kaspisa.db.models import ShopConnectionState
from kaspisa.kaspi import (
    CodeRequired,
    KaspiSession,
    KaspiTransportError,
    LoggedIn,
    LoginRejectedError,
    PriceWriteOutcomeUnknownError,
    RateLimitedError,
    ResponseFormatError,
    SessionExpiredError,
    SessionStateError,
    UnexpectedStatusError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.kaspi.connection import (
    ConnectionChange,
    ConnectionOperation,
    ConnectionState,
    ensure_operation_allowed,
    next_connection_state,
)
from kaspisa.kaspi.models import ProductState

STATES: tuple[ConnectionState, ...] = get_args(ConnectionState)
OPERATIONS: tuple[ConnectionOperation, ...] = get_args(ConnectionOperation)

ALLOWED: set[tuple[str, str]] = {
    ("not_connected", "login"),
    ("awaiting_code", "login_restart"),
    ("awaiting_code", "submit_code"),
    ("awaiting_code", "resend_code"),
    ("connected", "cabinet"),
    ("reauth_required", "login"),
}


def _logged_in() -> LoggedIn:
    return LoggedIn(session=authenticated_session(), merchants=[])


def _code_required() -> CodeRequired:
    return CodeRequired(
        session=KaspiSession.load(session_blob(phase="awaiting_code", pending_login="m@e.invalid"))
    )


def _rejected(code: str) -> LoginRejectedError:
    return LoginRejectedError(code)


def test_table_has_twenty_pairs_six_allowed() -> None:
    """Т65 (1): 4 состояния x 5 операций = 20 пар, из них допустимы 6."""
    pairs = list(itertools.product(STATES, OPERATIONS))
    assert len(pairs) == 20
    assert len(ALLOWED) == 6


@pytest.mark.parametrize(("state", "operation"), list(itertools.product(STATES, OPERATIONS)))
def test_ensure_operation_allowed_table(state: ConnectionState, operation: str) -> None:
    """Т65 (1): допустимые пары проходят, остальные - `SessionStateError`."""
    op: Any = operation
    if (state, operation) in ALLOWED:
        ensure_operation_allowed(state, op)
    else:
        with pytest.raises(SessionStateError):
            ensure_operation_allowed(state, op)


def _change(state: ConnectionState, error: str | None = None) -> ConnectionChange:
    return ConnectionChange(state=state, error=error)


_TEMPORARY = ("TIMEOUT_EXCEEDED", "MFA_SEND_FLOOD", "MFA_CODE_TOO_MANY_SEND")
_LOGIN_PAIRS: tuple[tuple[ConnectionState, ConnectionOperation], ...] = (
    ("not_connected", "login"),
    ("reauth_required", "login"),
    ("awaiting_code", "login_restart"),
    ("awaiting_code", "submit_code"),
    ("awaiting_code", "resend_code"),
)


@pytest.mark.parametrize(
    ("state", "operation", "outcome_factory", "expected"),
    [
        ("not_connected", "login", _logged_in, _change("connected")),
        ("reauth_required", "login", _logged_in, _change("connected")),
        ("awaiting_code", "login_restart", _logged_in, _change("connected")),
        ("awaiting_code", "submit_code", _logged_in, _change("connected")),
        ("not_connected", "login", _code_required, _change("awaiting_code")),
        ("reauth_required", "login", _code_required, _change("awaiting_code")),
        ("awaiting_code", "login_restart", _code_required, _change("awaiting_code")),
        (
            "awaiting_code",
            "submit_code",
            lambda: _rejected("MFA_CODE_INVALID"),
            None,
        ),
        (
            "awaiting_code",
            "submit_code",
            lambda: _rejected("MFA_CODE_ATTEMPT_LIMIT"),
            _change("reauth_required", "MFA_CODE_ATTEMPT_LIMIT"),
        ),
        (
            "not_connected",
            "login",
            lambda: _rejected("CREDENTIALS_INVALID"),
            _change("reauth_required", "CREDENTIALS_INVALID"),
        ),
        (
            "reauth_required",
            "login",
            lambda: _rejected("FAILED"),
            _change("reauth_required", "FAILED"),
        ),
        (
            "awaiting_code",
            "login_restart",
            lambda: _rejected("STAFF_MODE"),
            _change("reauth_required", "STAFF_MODE"),
        ),
        (
            "awaiting_code",
            "resend_code",
            lambda: _rejected("SOMETHING_NEW"),
            _change("reauth_required", "SOMETHING_NEW"),
        ),
        # Код неверного кода допустим как «не менять» только у `submit_code`.
        (
            "not_connected",
            "login",
            lambda: _rejected("MFA_CODE_INVALID"),
            _change("reauth_required", "MFA_CODE_INVALID"),
        ),
        (
            "connected",
            "cabinet",
            lambda: SessionExpiredError("get_product"),
            _change("reauth_required", "session_expired"),
        ),
    ],
)
def test_next_state_rules(
    state: ConnectionState,
    operation: ConnectionOperation,
    outcome_factory: Any,
    expected: ConnectionChange | None,
) -> None:
    """Т65 (2): по строке на правило 7.3."""
    assert next_connection_state(state, operation, outcome_factory()) == expected


@pytest.mark.parametrize("code", _TEMPORARY)
@pytest.mark.parametrize(("state", "operation"), _LOGIN_PAIRS)
def test_temporary_login_codes_change_nothing(
    state: ConnectionState, operation: ConnectionOperation, code: str
) -> None:
    """Т65 (2): временные отказы входа - `None` для каждой операции входа."""
    assert next_connection_state(state, operation, _rejected(code)) is None


def _neutral_outcomes() -> list[object]:
    return [
        RateLimitedError("op", 30),
        KaspiTransportError("op", "ReadTimeout"),
        UnexpectedStatusError("op", 500),
        ResponseFormatError("op", ("field",)),
        WriteRefusedError(WriteRefusal.PREORDER, "sku"),
        PriceWriteOutcomeUnknownError("sku", "ReadTimeout"),
        ProductState.model_validate(load_fixture("edit-price.json")),
        None,
    ]


@pytest.mark.parametrize("outcome", _neutral_outcomes(), ids=lambda o: type(o).__name__)
def test_neutral_outcomes_change_nothing_for_cabinet(outcome: object) -> None:
    """Т65 (2): прочие исходы допустимой пары - `None`."""
    assert next_connection_state("connected", "cabinet", outcome) is None


@pytest.mark.parametrize(
    "outcome",
    [
        RateLimitedError("op", 30),
        KaspiTransportError("op", "ReadTimeout"),
        UnexpectedStatusError("op", 500),
        ResponseFormatError("op", ("field",)),
        None,
    ],
    ids=lambda o: type(o).__name__,
)
@pytest.mark.parametrize(("state", "operation"), _LOGIN_PAIRS)
def test_neutral_outcomes_change_nothing_for_login(
    state: ConnectionState, operation: ConnectionOperation, outcome: object
) -> None:
    """Т65 (2): сетевые и прочие сбои входа, а также `None` от `resend_code` - `None`."""
    assert next_connection_state(state, operation, outcome) is None


@pytest.mark.parametrize(
    ("state", "operation", "outcome_factory"),
    [
        ("connected", "login", _logged_in),
        ("awaiting_code", "login", _code_required),
        ("not_connected", "cabinet", lambda: SessionExpiredError("op")),
        ("connected", "submit_code", _logged_in),
        ("reauth_required", "resend_code", lambda: None),
    ],
)
def test_forbidden_pair_raises(
    state: ConnectionState, operation: ConnectionOperation, outcome_factory: Any
) -> None:
    """Т65 (3): недопустимая пара - `SessionStateError`, каким бы ни был исход."""
    with pytest.raises(SessionStateError):
        next_connection_state(state, operation, outcome_factory())


@pytest.mark.parametrize(
    ("state", "operation", "outcome_factory"),
    [
        ("connected", "cabinet", _logged_in),
        ("connected", "cabinet", _code_required),
        ("awaiting_code", "resend_code", _logged_in),
        ("awaiting_code", "resend_code", _code_required),
        ("awaiting_code", "submit_code", _code_required),
        ("awaiting_code", "submit_code", lambda: SessionExpiredError("op")),
        ("not_connected", "login", lambda: SessionExpiredError("op")),
        ("connected", "cabinet", lambda: _rejected("FAILED")),
    ],
)
def test_impossible_outcome_raises(
    state: ConnectionState, operation: ConnectionOperation, outcome_factory: Any
) -> None:
    """Т65 (4): исход, невозможный для операции, - `SessionStateError` (правило 9 раньше 8)."""
    with pytest.raises(SessionStateError):
        next_connection_state(state, operation, outcome_factory())


def test_error_only_for_reauth_required() -> None:
    """У `ConnectionChange` ошибка непуста только для `reauth_required`."""
    for state, operation in _LOGIN_PAIRS:
        for outcome in (_logged_in(), _code_required(), _rejected("FAILED"), None):
            try:
                change = next_connection_state(state, operation, outcome)
            except SessionStateError:
                continue
            if change is not None and change.state != "reauth_required":
                assert change.error is None


def test_state_literals_match_database_enum() -> None:
    """Т66: литералы `ConnectionState` равны значениям `ShopConnectionState` (базу не открываем)."""
    assert set(get_args(ConnectionState)) == {member.value for member in ShopConnectionState}
    assert typing.get_origin(ConnectionState) is typing.Literal
