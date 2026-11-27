"""Т39 этапа 19: Telegram и удаление магазина доступны только администратору (ADR 0039, Н2)."""

from __future__ import annotations

import respx
from api_notify_testing import T2, T, notifier_of, owner, sent_messages, shop_of, url_of
from api_testing import (
    api_cipher,
    api_database_url,
    confirm_password,
    deps,
    raw_engine,
    redis_client,
    redis_prefix,
    signed_in_member,
    sql_scalar,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from kaspisa.api import ApiDeps

__all__ = [
    "api_cipher",
    "api_database_url",
    "deps",
    "raw_engine",
    "redis_client",
    "redis_prefix",
]

ADMIN = "admin@example.com"
NEW_SETTINGS = {"token": T2, "chat_id": "43"}


async def test_member_cannot_change_telegram_or_delete_shop(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    admin, tenant = await owner(deps, raw_engine, ADMIN)
    shop = await shop_of(deps, raw_engine, tenant)
    staff, _ = await signed_in_member(deps, ADMIN, "staff@example.com")
    events_before = await sql_scalar(raw_engine, "SELECT count(*) FROM owner_event")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        mock.post(url_of(T2)).respond(200)
        for confirmed in (False, True):
            if confirmed:
                await confirm_password(staff)
            for response in (
                await staff.put("/api/notifications/telegram", json=NEW_SETTINGS),
                await staff.delete("/api/notifications/telegram"),
                await staff.delete(f"/api/shops/{shop}"),
            ):
                assert response.status_code == 403, response.text
                assert response.json()["code"] == "admin_required"
        await notifier_of(deps).drain()
        assert sent_messages(mock) == []

    assert (await admin.get("/api/notifications/telegram")).json() == {
        "token_set": True,
        "chat_id": "42",
    }
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM shop") == 1
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM owner_event") == events_before


async def test_member_can_read_and_test_telegram(deps: ApiDeps, raw_engine: AsyncEngine) -> None:
    await owner(deps, raw_engine, ADMIN)
    staff, _ = await signed_in_member(deps, ADMIN, "staff@example.com")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        read = await staff.get("/api/notifications/telegram")
        check = await staff.post("/api/notifications/telegram/test")

    assert read.status_code == 200
    assert read.json() == {"token_set": True, "chat_id": "42"}
    assert check.status_code == 204


async def test_admin_still_changes_telegram_and_deletes_shop(
    deps: ApiDeps, raw_engine: AsyncEngine
) -> None:
    admin, tenant = await owner(deps, raw_engine, ADMIN, confirmed=True)
    shop = await shop_of(deps, raw_engine, tenant)

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url_of(T)).respond(200)
        changed = await admin.put("/api/notifications/telegram", json=NEW_SETTINGS)
        removed = await admin.delete("/api/notifications/telegram")
        deleted = await admin.delete(f"/api/shops/{shop}")
        await notifier_of(deps).drain()

    assert changed.status_code == 200
    assert removed.status_code == 204
    assert deleted.status_code == 204
    assert await sql_scalar(raw_engine, "SELECT count(*) FROM shop") == 0
