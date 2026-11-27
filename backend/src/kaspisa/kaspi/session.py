"""Сессия кабинета магазина: cookie, фаза входа и сохраняемый блок.

Cookie живут только здесь: общий `httpx.AsyncClient` их не хранит (изоляция магазинов).
Наружу сессия отдаёт себя только как `Secret` (`dump`); `repr` значений не раскрывает.
"""

from __future__ import annotations

import copy
import json
from enum import StrEnum
from http.cookiejar import Cookie, DefaultCookiePolicy
from typing import Any, NoReturn

import httpx

from kaspisa.kaspi.errors import SessionBlobError, SessionStateError
from kaspisa.security import Secret

BLOB_VERSION = 1
_ALLOWED_DOMAINS = [".kaspi.kz", "kaspi.kz"]


class SessionPhase(StrEnum):
    """Фаза сессии: объект создан, ждёт код из письма, вошёл."""

    NEW = "new"
    AWAITING_CODE = "awaiting_code"
    AUTHENTICATED = "authenticated"


_SAVED_PHASES = {SessionPhase.AWAITING_CODE.value, SessionPhase.AUTHENTICATED.value}

# Строка cookie в сохраняемом блоке: имя, значение, домен, путь, secure, expires, host_only.
_CookieKey = tuple[str, str, str, str, bool, int | None, bool]


def is_kaspi_domain(domain: str) -> bool:
    """Домен `kaspi.kz` или его поддомен (с точкой в начале или без)."""
    bare = domain.lower().lstrip(".")
    return bare == "kaspi.kz" or bare.endswith(".kaspi.kz")


def _new_jar() -> httpx.Cookies:
    cookies = httpx.Cookies()
    cookies.jar.set_policy(
        DefaultCookiePolicy(
            allowed_domains=_ALLOWED_DOMAINS,
            strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
        )
    )
    return cookies


class KaspiSession:
    """Состояние входа магазина в кабинет Kaspi.

    Изменяема: если Kaspi обновил cookie, `changed` становится `True` и вызывающий
    сохраняет `dump()`. Одной сессией одновременно пользуется одна задача.
    """

    __slots__ = ("_cookies", "_pending_login", "_phase", "changed")

    def __init__(self) -> None:
        """Новая сессия в фазе `NEW`. Другие фазы наступают только входом или `load()`."""
        self._cookies = _new_jar()
        self._phase = SessionPhase.NEW
        self._pending_login: str | None = None
        self.changed = False

    @property
    def phase(self) -> SessionPhase:
        return self._phase

    def _cookie_keys(self) -> set[_CookieKey]:
        return {
            (
                c.name,
                c.value or "",
                c.domain,
                c.path,
                bool(c.secure),
                c.expires,
                not c.domain_specified,
            )
            for c in self._cookies.jar
        }

    def apply_cookies(self, request: httpx.Request) -> None:
        """Ставит заголовок `Cookie` из подходящих по домену и пути cookie сессии."""
        self._cookies.set_cookie_header(request)

    def absorb(self, response: httpx.Response) -> None:
        """Забирает `Set-Cookie` ответа (только домены Kaspi); при изменении ставит `changed`."""
        before = self._cookie_keys()
        self._cookies.extract_cookies(response)
        for cookie in list(self._cookies.jar):
            if not is_kaspi_domain(cookie.domain):
                self._cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
        if self._cookie_keys() != before:
            self.changed = True

    # Методы с подчёркиванием вызывает только `login.py`: вызывающий код фазу не меняет.

    def _copy_cookies_from(self, other: KaspiSession) -> None:
        """Копирует cookie (в том числе запомненного устройства) из прежней сессии."""
        for cookie in other._cookies.jar:
            self._cookies.jar.set_cookie(copy.copy(cookie))

    def _pending(self) -> str | None:
        return self._pending_login

    def _mark_code_sent(self, email: str) -> None:
        """Kaspi отправил код на `email`: фаза `AWAITING_CODE`."""
        self._pending_login = email
        self._phase = SessionPhase.AWAITING_CODE

    def _mark_authenticated(self) -> None:
        """Вход завершён и подтверждён запросом кабинета: фаза `AUTHENTICATED`."""
        self._pending_login = None
        self._phase = SessionPhase.AUTHENTICATED

    def dump(self) -> Secret:
        """Блок для `session_enc`: JSON с версией, фазой и всеми cookie доменов Kaspi."""
        if self._phase is SessionPhase.NEW:
            raise SessionStateError("сессия ещё не начата: сохранять нечего")
        cookies = [
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure,
                "expires": expires,
                "host_only": host_only,
            }
            for name, value, domain, path, secure, expires, host_only in sorted(
                self._cookie_keys(), key=lambda key: key[:4]
            )
            if is_kaspi_domain(domain)
        ]
        blob = {
            "v": BLOB_VERSION,
            "phase": self._phase.value,
            "pending_login": self._pending_login,
            "cookies": cookies,
        }
        return Secret(json.dumps(blob, ensure_ascii=False))

    @classmethod
    def load(cls, blob: Secret) -> KaspiSession:
        """Разбирает блок из `session_enc`; любая ошибка - `SessionBlobError` без содержимого."""
        session = cls._parse(blob.reveal())
        if session is None:
            raise SessionBlobError("сохранённый блок сессии не разобран")
        return session

    @classmethod
    def _parse(cls, raw: str) -> KaspiSession | None:
        data: Any
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("v") != BLOB_VERSION:
            return None
        phase, pending, cookies = data.get("phase"), data.get("pending_login"), data.get("cookies")
        if phase not in _SAVED_PHASES or not isinstance(cookies, list):
            return None
        if pending is not None and not isinstance(pending, str):
            return None
        session = cls()
        session._phase = SessionPhase(phase)
        session._pending_login = pending
        for item in cookies:
            cookie = _cookie_from_blob(item)
            if cookie is None:
                return None
            session._cookies.jar.set_cookie(cookie)
        return session

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, KaspiSession):
            return NotImplemented
        return (
            self._phase is other._phase
            and self._pending_login == other._pending_login
            and self._cookie_keys() == other._cookie_keys()
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"KaspiSession(phase={self._phase.value}, cookies={len(self._cookie_keys())})"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return format(repr(self), format_spec)

    def __reduce__(self) -> NoReturn:
        raise TypeError("сессию нельзя сериализовать или копировать: только dump()")


def _cookie_from_blob(item: Any) -> Cookie | None:
    if not isinstance(item, dict):
        return None
    name, value, domain, path = (
        item.get("name"),
        item.get("value"),
        item.get("domain"),
        item.get("path"),
    )
    secure, expires, host_only = item.get("secure"), item.get("expires"), item.get("host_only")
    if not (
        isinstance(name, str)
        and isinstance(value, str)
        and isinstance(domain, str)
        and isinstance(path, str)
    ):
        return None
    if not isinstance(secure, bool) or not isinstance(host_only, bool):
        return None
    if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int)):
        return None
    if not name or not is_kaspi_domain(domain) or (host_only and domain.startswith(".")):
        return None
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=not host_only,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={},
    )
