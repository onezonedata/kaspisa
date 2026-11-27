"""Транспорт: единственное место, где вызывается `httpx`.

Здесь живут ограничитель частоты и его ключ, повторы, изоляция cookie, превращение сбоев
в собственные исключения и лог запросов.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from kaspisa.kaspi.errors import (
    KaspiTransportError,
    RateLimitedError,
    ResponseFormatError,
    UnexpectedStatusError,
)
from kaspisa.kaspi.limiter import IntervalRateLimiter
from kaspisa.kaspi.models import parse_model
from kaspisa.kaspi.session import KaspiSession

logger = logging.getLogger("kaspisa.kaspi")

ALLOWED_HOSTS = frozenset({"kaspi.kz", "mc.shop.kaspi.kz", "idmc.shop.kaspi.kz"})
RETRY_STATUSES = frozenset({502, 503, 504})
RETRY_PAUSES = (1.0, 3.0)
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 15.0
# Постоянная строка настольного браузера: запросы сняты из браузера (предположение П8).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Reply:
    """Ответ Kaspi внутри клиента. Запрос в нём не хранится.

    Заголовки содержат `Set-Cookie`, тело входа - e-mail, поэтому `repr` показывает только статус.
    """

    status_code: int
    headers: httpx.Headers
    content: bytes

    def __repr__(self) -> str:
        return f"Reply(status_code={self.status_code})"

    __str__ = __repr__

    def json(self) -> Any:
        """Тело как JSON; ошибка разбора пробрасывается как `ValueError`."""
        return json.loads(self.content)


def limiter_key_for(host: str, merchant_uid: str | None) -> str:
    """Ключ ограничителя по хосту (правило частоты задано по хостам)."""
    host = host.lower()
    if host == "mc.shop.kaspi.kz":
        return f"mc:{merchant_uid}" if merchant_uid else "mc:unknown"
    return host


def classify[M: BaseModel](reply: Reply, operation: str, model: type[M]) -> M:
    """Общие проверки ответа публичной карточки и разбор тела.

    Порядок: 429, статус вне 2xx, тело - JSON и подходит модели.
    """
    if reply.status_code == 429:
        raise RateLimitedError(operation, parse_retry_after(reply))
    if not 200 <= reply.status_code <= 299:
        raise UnexpectedStatusError(operation, reply.status_code)
    return parse_body(reply, operation, model)


def parse_body[M: BaseModel](reply: Reply, operation: str, model: type[M]) -> M:
    """Разбирает тело ответа моделью; не JSON или ошибка модели - `ResponseFormatError`."""
    data: Any = None
    ok = False
    try:
        data = reply.json()
        ok = True
    except (ValueError, RecursionError):
        pass
    if not ok:
        raise ResponseFormatError(operation, ("<body>",))
    return parse_model(model, data, operation)


def parse_retry_after(reply: Reply) -> int | None:
    """Целое число секунд из `Retry-After` или `None`."""
    raw = reply.headers.get("Retry-After")
    if raw is None:
        return None
    value = raw.strip()
    # isdigit() пропускает «²» и арабские цифры, int() на них падает: берём только ASCII.
    if value.isascii() and value.isdigit():
        return int(value)
    return None


class Transport:
    """Отправляет запросы к Kaspi через общий `httpx.AsyncClient` без своих cookie."""

    def __init__(
        self,
        limiter: IntervalRateLimiter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._limiter = limiter
        self._sleep = sleep
        # Банка cookie, которая ничего не принимает: сессия одного магазина не уйдёт другому.
        jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        self._http = httpx.AsyncClient(
            follow_redirects=False,
            cookies=jar,
            timeout=REQUEST_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def send(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        retry: bool,
        merchant_uid: str | None = None,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        params: Mapping[str, str | int] | None = None,
        session: KaspiSession | None = None,
        hide_path: bool = False,
    ) -> Reply:
        """Один запрос с ограничителем и (если `retry`) повторами при сбое и 502/503/504.

        Cookie берутся только из `session` и туда же возвращаются из `Set-Cookie` ответа.
        """
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in ALLOWED_HOSTS:
            raise ValueError("адрес вне списка хостов Kaspi или не https")
        key = limiter_key_for(host, merchant_uid)
        attempts = MAX_ATTEMPTS if retry else 1
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        # Путь в лог не пишем у переходов входа: в адресе бывает одноразовый код возврата.
        path = "" if hide_path else parts.path
        kind: str | None = None
        for attempt in range(1, attempts + 1):
            await self._limiter.acquire(key)
            kind = None
            retryable = True
            reply: Reply | None = None
            try:
                request = self._http.build_request(
                    method, url, headers=merged, json=json, params=params
                )
                if session is not None:
                    session.apply_cookies(request)
                response = await self._http.send(request)
                if session is not None:
                    session.absorb(response)
                reply = Reply(response.status_code, response.headers, response.content)
            except (httpx.TransportError, httpx.DecodingError) as exc:
                kind = type(exc).__name__  # сам объект не сохраняется: в нём заголовки и тело
            except Exception as exc:  # прочие сбои не повторяются; отмена (не Exception) проходит
                kind = type(exc).__name__
                retryable = False
                # Не сбой сети, а вероятная ошибка своего кода (К11): только класс, без текста.
                logger.warning("kaspi %s: непредвиденный сбой %s", operation, kind)
            logger.info(
                "kaspi %s %s %s%s -> %s (попытка %d)",
                operation,
                method,
                host,
                path,
                reply.status_code if reply is not None else kind,
                attempt,
            )
            last = attempt == attempts
            if reply is not None:
                if retry and reply.status_code in RETRY_STATUSES and not last:
                    await self._sleep(RETRY_PAUSES[attempt - 1])
                    continue
                return reply
            if not retryable:
                break
            if not last:
                await self._sleep(RETRY_PAUSES[attempt - 1])
        raise KaspiTransportError(operation, kind or "unknown")
