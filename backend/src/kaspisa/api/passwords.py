"""Хеш пароля сервиса: scrypt из стандартной библиотеки, без новых зависимостей."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import unicodedata
import weakref
from collections.abc import Callable

_N = 16384
_R = 8
_P = 5
_SALT_BYTES = 16
_HASH_BYTES = 32
_MAXMEM = 64 * 1024 * 1024
_MAX_N = 2**20
_MAX_R = 32
_MAX_P = 16
HASH_CONCURRENCY = 4
HASH_QUEUE_LIMIT = 16  # сверх выполняющихся: дальше сразу `server_busy`


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    normalized = unicodedata.normalize("NFKC", password).encode()
    return hashlib.scrypt(normalized, salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=length)


def hash_password(password: str) -> str:
    """Строка `scrypt$N$r$p$соль$хеш`; параметры читаются из неё при проверке."""
    salt = os.urandom(_SALT_BYTES)
    digest = _derive(password, salt, _N, _R, _P, _HASH_BYTES)
    return "$".join(
        [
            "scrypt",
            str(_N),
            str(_R),
            str(_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Сравнение за постоянное время; сломанная строка хеша даёт `False`."""
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, digest_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if not (1 < n <= _MAX_N and n & (n - 1) == 0 and 0 < r <= _MAX_R and 0 < p <= _MAX_P):
            return False
        salt = base64.b64decode(salt_raw, validate=True)
        expected = base64.b64decode(digest_raw, validate=True)
        if not salt or not expected:
            return False
        actual = _derive(password, salt, n, r, p, len(expected))
    except (ValueError, binascii.Error, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


DUMMY_HASH = hash_password("dummy-password-for-timing")


class HashBusyError(Exception):
    """Очередь хеширования полна: scrypt не считался, запрос нужно повторить позже."""


class _Gate:
    """Семафор и счётчик допущенных (идут и ждут) на один цикл событий."""

    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(HASH_CONCURRENCY)
        self.limit = HASH_CONCURRENCY + HASH_QUEUE_LIMIT
        self.admitted = 0


# Шлюз на цикл событий: тесты запускают по циклу на каждый тест.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Gate] = weakref.WeakKeyDictionary()


def _gate() -> _Gate:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _Gate()
        _gates[loop] = gate
    return gate


async def _run[**P, T](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    gate = _gate()
    if gate.admitted >= gate.limit:
        raise HashBusyError
    gate.admitted += 1
    try:
        async with gate.semaphore:
            return await asyncio.to_thread(function, *args, **kwargs)
    finally:
        gate.admitted -= 1


async def hash_password_async(password: str) -> str:
    return await _run(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    return await _run(verify_password, password, stored)
