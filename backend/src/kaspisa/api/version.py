"""Проверка новой версии: последний выпуск на GitHub с кэшем в памяти процесса (ADR 0023)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from fastapi import APIRouter

from kaspisa.api.deps import CurrentPrincipal, Deps
from kaspisa.api.schemas import VersionOut

logger = logging.getLogger("kaspisa.api")

router = APIRouter(prefix="/api")

CACHE_SECONDS = 86_400
REQUEST_TIMEOUT_SECONDS = 5
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_NUMBERS = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class _Cached:
    fetched_at: float
    latest: str | None


def _triple(match: re.Match[str]) -> tuple[int, int, int]:
    return int(match[1]), int(match[2]), int(match[3])


class ReleaseCheck:
    """Узнаёт последний выпуск; пустой `repo` выключает проверку (сети нет)."""

    def __init__(
        self,
        repo: str,
        current: str,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._repo = repo
        self._current = current
        self._clock = clock
        self._transport = transport
        self._cached: _Cached | None = None

    async def get(self) -> VersionOut:
        if not self._repo:
            return self._result(None)
        cached = self._cached
        if cached is None or self._clock() - cached.fetched_at >= CACHE_SECONDS:
            cached = _Cached(self._clock(), await self._fetch())
            self._cached = cached
        return self._result(cached.latest)

    def _result(self, latest: str | None) -> VersionOut:
        available = False
        if latest is not None:
            now = _NUMBERS.match(self._current)
            new = _NUMBERS.match(latest)
            available = bool(now and new and _triple(new) > _triple(now))
        url = f"https://github.com/{self._repo}/releases/tag/v{latest}" if latest else None
        return VersionOut(
            current=self._current, latest=latest, update_available=available, release_url=url
        )

    async def _fetch(self) -> str | None:
        """Версия `X.Y.Z` последнего выпуска или `None`; причина сбоя - в журнал без тела."""
        url = f"https://api.github.com/repos/{self._repo}/releases/latest"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "kaspisa"}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("version_check_failed: %s", type(exc).__name__)
            return None
        if response.status_code != 200:
            logger.warning("version_check_failed: status %s", response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning("version_check_failed: not_json")
            return None
        tag = body.get("tag_name") if isinstance(body, dict) else None
        match = _TAG.match(tag) if isinstance(tag, str) else None
        if match is None:
            logger.warning("version_check_failed: bad_tag")
            return None
        return ".".join(match.groups())


@router.get("/version")
async def get_version(principal: CurrentPrincipal, deps: Deps) -> VersionOut:
    """Текущая версия и признак новой; нужна сессия."""
    return await deps.release_check.get()
