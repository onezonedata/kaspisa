"""Точка входа HTTP API: `uvicorn kaspisa.main:app`."""

import logging
import sys

from kaspisa.api.app import app

__all__ = ["app", "configure_logging"]

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_API_LOGGERS = ("kaspisa.kaspi", "kaspisa.api")
_QUIET_LOGGERS = ("httpx", "httpcore", "hpack")


def configure_logging() -> None:
    """Выводит журналы клиента Kaspi и API в stderr на уровне INFO (ADR 0017).

    Клиент и API секретов не пишут: только операции, коды и классы исключений. Прочие
    логгеры (`kaspisa.db`, `kaspisa.worker`, корневой, `uvicorn`) не трогаются. Повторный
    вызов второй обработчик не добавляет; `propagate` остаётся включённым.
    """
    for name in _API_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
            logger.addHandler(handler)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


configure_logging()
