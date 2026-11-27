"""Помощники тестов уведомлений Telegram: токены, разбор запросов, начальные данные."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import respx
from api_testing import FakeClock, confirm_password, seed_shop, signed_in, tenant_of
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps
from kaspisa.api.telegram import Notifier
from kaspisa.db import TenantId
from kaspisa.security import Secret

T = "123456789:" + "A" * 35
T2 = "987654321:" + "B" * 35
API = "https://api.telegram.org"
GITHUB = "https://api.github.com/repos/o/r/releases/latest"


def url_of(token: str) -> str:
    return f"{API}/bot{token}/sendMessage"


@dataclass(frozen=True)
class Sent:
    token: str
    chat_id: str
    text: str


def sent_messages(mock: respx.MockRouter) -> list[Sent]:
    """Запросы в Telegram, в порядке отправки."""
    result = []
    for call in mock.calls:
        if call.request.url.host != "api.telegram.org":
            continue
        body = json.loads(call.request.content)
        token = call.request.url.path.removeprefix("/bot").removesuffix("/sendMessage")
        result.append(Sent(token, body["chat_id"], body["text"]))
    return result


def notifier_of(deps: ApiDeps) -> Notifier:
    assert deps.notifier is not None
    return deps.notifier


def clock_of(deps: ApiDeps) -> FakeClock:
    assert isinstance(deps.clock, FakeClock)
    return deps.clock


async def save_telegram(
    deps: ApiDeps, tenant: TenantId, *, token: str = T, chat_id: str = "42"
) -> None:
    async with deps.database.tenant(tenant) as scope:
        await scope.telegram.save(chat_id=chat_id, token=Secret(token))
        await scope.commit()


async def owner(
    deps: ApiDeps,
    raw: AsyncEngine,
    email: str = "a@example.com",
    *,
    ip: str = "127.0.0.1",
    token: str = T,
    chat_id: str = "42",
    confirmed: bool = False,
) -> tuple[httpx.AsyncClient, TenantId]:
    """Зарегистрированный пользователь с настройками Telegram."""
    client = await signed_in(deps, email, ip=ip)
    tenant = TenantId(await tenant_of(raw, email))
    await save_telegram(deps, tenant, token=token, chat_id=chat_id)
    if confirmed:
        await confirm_password(client)
    return client, tenant


async def shop_of(
    deps: ApiDeps, raw: AsyncEngine, tenant: TenantId, uid: str = "30001", **settings: Any
) -> uuid.UUID:
    return await seed_shop(
        deps, raw, tenant, state="connected", merchant_uid=uid, version=1, **settings
    )


async def halt(deps: ApiDeps, tenant: TenantId, shop: uuid.UUID, reason: str, at: datetime) -> None:
    async with deps.database.tenant(tenant) as scope:
        await scope.shops.halt(shop, reason, at)
        await scope.commit()
