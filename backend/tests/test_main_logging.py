"""ADR 0017, Т17: журнал процесса API (`kaspisa.kaspi` и `kaspisa.api` на INFO)."""

import logging
import subprocess
import sys
from pathlib import Path

from kaspisa.main import configure_logging

BACKEND = Path(__file__).resolve().parents[1]


def test_configure_logging_is_idempotent_and_narrow() -> None:
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    configure_logging()
    configure_logging()
    for name in ("kaspisa.kaspi", "kaspisa.api"):
        logger = logging.getLogger(name)
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert logger.propagate is True
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert logging.getLogger("hpack").level == logging.WARNING
    for name in ("kaspisa.db", "kaspisa.worker"):
        assert logging.getLogger(name).level == logging.NOTSET


def test_importing_the_entry_point_configures_logging() -> None:
    """`uvicorn kaspisa.main:app` настраивает журнал уже при импорте."""
    code = (
        "import logging, kaspisa.main;"
        "k = logging.getLogger('kaspisa.kaspi');"
        "print(k.level, len(k.handlers), len(logging.getLogger('kaspisa.api').handlers))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert result.stdout.split() == [str(logging.INFO), "1", "1"]
