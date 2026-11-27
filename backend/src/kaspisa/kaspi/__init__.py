"""Клиент Kaspi: весь доступ к неофициальным запросам идёт через этот пакет."""

from __future__ import annotations

import logging

# Эти логгеры пишут адреса запросов и (httpcore на DEBUG) заголовки ответов с cookie сессии.
for _name in ("httpx", "httpcore", "hpack"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from kaspisa.kaspi.cabinet import (  # noqa: E402
    PRICE_VERIFY_DEADLINE_SECONDS,
    PRICE_VERIFY_FIRST_CHECK_SECONDS,
    PRICE_WRITE_HOLD_SECONDS,
    reset_points,
)
from kaspisa.kaspi.client import KaspiClient  # noqa: E402
from kaspisa.kaspi.connection import (  # noqa: E402
    ConnectionChange,
    ConnectionOperation,
    ConnectionState,
    ensure_operation_allowed,
    next_connection_state,
)
from kaspisa.kaspi.errors import (  # noqa: E402
    KaspiError,
    KaspiTransportError,
    LoginRejectedError,
    PriceWriteOutcomeUnknownError,
    RateLimitedError,
    ResponseFormatError,
    SessionBlobError,
    SessionExpiredError,
    SessionStateError,
    UnexpectedStatusError,
    WriteRefusal,
    WriteRefusedError,
)
from kaspisa.kaspi.limiter import IntervalRateLimiter  # noqa: E402
from kaspisa.kaspi.models import CodeRequired, KaspiCredentials, LoggedIn  # noqa: E402
from kaspisa.kaspi.session import KaspiSession, SessionPhase  # noqa: E402

__all__ = [
    "PRICE_VERIFY_DEADLINE_SECONDS",
    "PRICE_VERIFY_FIRST_CHECK_SECONDS",
    "PRICE_WRITE_HOLD_SECONDS",
    "CodeRequired",
    "ConnectionChange",
    "ConnectionOperation",
    "ConnectionState",
    "IntervalRateLimiter",
    "KaspiClient",
    "KaspiCredentials",
    "KaspiError",
    "KaspiSession",
    "KaspiTransportError",
    "LoggedIn",
    "LoginRejectedError",
    "PriceWriteOutcomeUnknownError",
    "RateLimitedError",
    "ResponseFormatError",
    "SessionBlobError",
    "SessionExpiredError",
    "SessionPhase",
    "SessionStateError",
    "UnexpectedStatusError",
    "WriteRefusal",
    "WriteRefusedError",
    "ensure_operation_allowed",
    "next_connection_state",
    "reset_points",
]
