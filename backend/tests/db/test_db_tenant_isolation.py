"""Т18–Т24: изоляция арендаторов. Проверка идёт из области Б над строками А."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from db_seed import TwoTenants
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import (
    ConstraintViolationError,
    Database,
    NotFoundError,
    OwnerEventKind,
    OwnerEventRepository,
    PriceChangeRepository,
    PriceChangeResult,
    PriceWriteRepository,
    ProductRepository,
    ShopRepository,
    TelegramRepository,
    UserRepository,
)
from kaspisa.security import Secret

CHECKED: dict[type, set[str]] = {
    ShopRepository: {
        "add",
        "get",
        "list",
        "delete",
        "get_credentials",
        "set_credentials",
        "get_session",
        "save_session",
        "mark_awaiting_code",
        "mark_connected",
        "mark_reauth_required",
        # обработчик (этап 5): тесты изоляции — test_db_worker_claim.py, test_db_worker_repos.py
        "release_lease",
        "renew_lease",
        "defer",
        "record_login_outcome",
        "record_write_outcome",
        "halt",
        "clear_halt",
        "lock_for_write",
        "clear_unproven_login",
        # суточная загрузка каталога (Н7)
        "mark_catalog_synced",
        "defer_catalog",
        # блокировка строки арендатора (этап 20): изоляция — test_db_single_shop.py
        "lock_owner",
    },
    ProductRepository: {
        "add",
        "get",
        "get_for_update",
        "get_by_sku",
        "list_for_shop",
        "count_for_shop",
        "next_due",
        "record_observation",
        "record_skip",  # изоляция: test_db_skip_reason.py
        "record_write",  # изоляция: test_db_pricing_rules.py
        "touch_checked",
        "set_refusal",
        "clear_refusal",
    },
    PriceChangeRepository: {
        "add",
        "list_for_shop",
        "list_for_product",
        "set_result",
        "count_sent_since",
        "has_sent_since",
        "quota_free_at",
        "discard_unsent",
    },
    PriceWriteRepository: {
        "get",
        "open",
        "set_status",
        "delete",
        "due_for_verification",
        "next_verify_time",
    },
    UserRepository: {"add", "get", "list"},
    OwnerEventRepository: {"add", "list_recent"},
    TelegramRepository: {"get", "get_target", "save", "delete"},  # Т5: ниже
}


async def test_telegram_repository_isolation(database: Database, two_tenants: TwoTenants) -> None:
    """Т5: настройки Telegram видны и меняются только своим арендатором."""
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(b.tenant_id) as scope:
        await scope.telegram.save(chat_id="42", token=Secret("123456789:" + "A" * 35))
        await scope.commit()
    async with database.tenant(a.tenant_id) as scope:
        assert await scope.telegram.get() is None
        assert await scope.telegram.get_target() is None
        with pytest.raises(NotFoundError):
            await scope.telegram.save(chat_id="7", token=None)
        assert await scope.telegram.delete() is False
        await scope.commit()
    async with database.tenant(b.tenant_id) as scope:
        target = await scope.telegram.get_target()
        assert target is not None
        assert target.chat_id == "42"


async def test_shop_repository_isolation(database: Database, two_tenants: TwoTenants) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(b.tenant_id) as scope:
        shops = scope.shops
        assert await shops.get(a.shop_id) is None
        assert a.shop_id not in [s.id for s in await shops.list()]
        version = 2  # верная версия чужого магазина: ошибка всё равно NotFoundError
        calls: dict[str, Callable[[], Awaitable[object]]] = {
            "delete": lambda: shops.delete(a.shop_id),
            "get_credentials": lambda: shops.get_credentials(a.shop_id),
            "set_credentials": lambda: shops.set_credentials(
                a.shop_id, login=Secret("x"), password=Secret("y")
            ),
            "get_session": lambda: shops.get_session(a.shop_id),
            "save_session": lambda: shops.save_session(
                a.shop_id, Secret("s"), expected_version=version
            ),
            "mark_awaiting_code": lambda: shops.mark_awaiting_code(
                a.shop_id, Secret("s"), expected_version=version
            ),
            "mark_connected": lambda: shops.mark_connected(
                a.shop_id, merchant_uid="m", session_state=Secret("s"), expected_version=version
            ),
            "mark_reauth_required": lambda: shops.mark_reauth_required(
                a.shop_id, error="e", expected_version=version
            ),
        }
        for call in calls.values():
            with pytest.raises(NotFoundError):
                await call()
    async with database.tenant(a.tenant_id) as scope:
        shop = await scope.shops.get(a.shop_id)
        assert shop is not None
        assert shop.connection_version == 1
        assert shop.connection_state == "connected"
        creds = await scope.shops.get_credentials(a.shop_id)
        assert creds.login.reveal() == a.login


async def test_product_repository_isolation(database: Database, two_tenants: TwoTenants) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(b.tenant_id) as scope:
        products = scope.products
        assert await products.get(a.product_id) is None
        assert await products.get_for_update(a.product_id) is None
        assert await products.get_by_sku(a.shop_id, a.sku) is None
        assert await products.list_for_shop(a.shop_id, limit=10) == []
        assert await products.count_for_shop(a.shop_id) == (0, 0)
        with pytest.raises(NotFoundError):
            await products.add(shop_id=a.shop_id, sku="x", master_sku="m", title="t")


async def test_price_change_repository_isolation(
    database: Database, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(b.tenant_id) as scope:
        changes = scope.price_changes
        assert await changes.list_for_shop(a.shop_id, limit=10) == []
        assert await changes.list_for_product(a.product_id, limit=10) == []
        with pytest.raises(NotFoundError):
            await changes.add(
                product_id=a.product_id,
                old_price=10,
                new_price=9,
                reason="r",
                result=PriceChangeResult.DRY_RUN,
            )


async def test_user_repository_isolation(database: Database, two_tenants: TwoTenants) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(b.tenant_id) as scope:
        assert await scope.users.get(a.user_id) is None
        assert a.user_id not in [u.id for u in await scope.users.list()]


async def test_owner_event_repository_isolation(
    database: Database, two_tenants: TwoTenants, connection: AsyncConnection
) -> None:
    """Т13: журнал владельца виден только своему арендатору; `tenant_id` ставит репозиторий."""
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(a.tenant_id) as scope:
        mine = await scope.owner_events.add(
            kind=OwnerEventKind.BOT_MODE_CHANGED,
            user_id=a.user_id,
            ip="10.0.0.1",
            shop_id=a.shop_id,
            old_value="off",
            new_value="live",
        )
        await scope.commit()
    assert mine.tenant_id == a.tenant_id
    async with database.tenant(b.tenant_id) as scope:
        assert list(await scope.owner_events.list_recent(10)) == []
        theirs = await scope.owner_events.add(
            kind=OwnerEventKind.LOGIN, user_id=b.user_id, ip="10.0.0.2"
        )
        await scope.commit()
    assert theirs.tenant_id == b.tenant_id
    async with database.tenant(a.tenant_id) as scope:
        assert [e.id for e in await scope.owner_events.list_recent(10)] == [mine.id]
    # смена tenant_id отвергается триггером, CHECK вида - ограничением
    for sql, fragment in (
        ("UPDATE owner_event SET tenant_id = :b WHERE id = :i", "tenant_id is immutable"),
        ("UPDATE owner_event SET kind = 'other' WHERE id = :i", "ck_owner_event_kind"),
    ):
        with pytest.raises(DBAPIError) as caught:
            async with connection.begin_nested():
                await connection.execute(text(sql), {"b": b.tenant_id, "i": mine.id})
        assert fragment in str(caught.value.orig)


async def test_owner_event_kind_and_shop_must_match(
    database: Database, two_tenants: TwoTenants
) -> None:
    a = two_tenants.a
    async with database.tenant(a.tenant_id) as scope:
        events = scope.owner_events
        with pytest.raises(ValueError, match="shop_id"):
            await events.add(
                kind=OwnerEventKind.LOGIN, user_id=a.user_id, ip="i", shop_id=a.shop_id
            )
        with pytest.raises(ValueError, match="shop_id"):
            await events.add(kind=OwnerEventKind.BOUNDS_CHANGED, user_id=a.user_id, ip="i")
        with pytest.raises(ValueError, match="shop_id"):
            await events.add(
                kind=OwnerEventKind.PASSWORD_CONFIRM_FAILED,
                user_id=a.user_id,
                ip="i",
                shop_id=a.shop_id,
            )


async def test_catalog_marks_isolation(database: Database, two_tenants: TwoTenants) -> None:
    a, b = two_tenants.a, two_tenants.b
    now = datetime(2030, 1, 1, tzinfo=UTC)
    async with database.tenant(b.tenant_id) as scope:
        with pytest.raises(NotFoundError):
            await scope.shops.mark_catalog_synced(a.shop_id, now)
        with pytest.raises(NotFoundError):
            await scope.shops.defer_catalog(a.shop_id, now)


def test_every_public_method_is_covered() -> None:
    """Новый публичный метод репозитория без теста изоляции не проходит."""
    for repo, checked in CHECKED.items():
        public = {
            n for n, m in inspect.getmembers(repo, inspect.isfunction) if not n.startswith("_")
        }
        assert public == checked, repo.__name__


async def test_system_scope(database: Database, two_tenants: TwoTenants) -> None:
    async with database.system() as system:
        found = await system.find_user_by_email(two_tenants.a.email.upper())
        assert found is not None
        assert found.id == two_tenants.a.user_id
        assert await system.find_user_by_email("nobody@example.kz") is None
        tenant = await system.create_tenant(name="новый")
        assert tenant.id is not None
        public = {n for n in dir(type(system)) if not n.startswith("_")}
        assert public == {
            "create_tenant",
            "find_user_by_email",
            "has_users",
            "register_first",
            "commit",
            "claim_shop",
            "unknown_key_spread",
            "pending_halt_notices",
            "mark_halt_notified",
            "pending_version_tenants",
            "mark_version_notified",
            "pending_stock_reset_notices",
            "mark_stock_reset_notified",
        }


async def test_tenant_id_assignment_rejected_through_model(
    database: Database, two_tenants: TwoTenants, connection: AsyncConnection
) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with database.tenant(a.tenant_id) as scope:
        user = await scope.users.get(a.user_id)
        assert user is not None
        user.tenant_id = b.tenant_id
        with pytest.raises(ConstraintViolationError) as info:
            await scope.commit()
        assert info.value.constraint == "tenant_id_immutable"
    async with database.tenant(a.tenant_id) as scope:
        shop = await scope.shops.get(a.shop_id)
        assert shop is not None
        shop.tenant_id = b.tenant_id
        with pytest.raises(ConstraintViolationError) as info:
            await scope.commit()
        assert info.value.constraint == "tenant_id_immutable"
    owner = await connection.execute(
        text("SELECT tenant_id FROM app_user WHERE id = :i"), {"i": a.user_id}
    )
    assert owner.scalar_one() == a.tenant_id
    assert uuid.UUID(str(a.tenant_id)) != b.tenant_id
