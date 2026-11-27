"""Т2-Т6 этапа 19: роль администратора, `delete_member`, снимок адреса автора события (ADR 0039)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from kaspisa.db import (
    ConstraintViolationError,
    Database,
    OwnerEventKind,
    TenantId,
)

HASH = "scrypt$stub"


async def _tenant(database: Database, name: str) -> TenantId:
    async with database.system() as system:
        tenant = await system.create_tenant(name=name)
        await system.commit()
    return TenantId(tenant.id)


async def _sql(url: str, sql: str, **params: object) -> list[tuple[object, ...]]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


async def test_register_first_makes_admin_and_add_makes_member(
    committed_database: Database,
) -> None:
    """Т2."""
    async with committed_database.system() as system:
        first = await system.register_first(email="owner@example.com", password_hash=HASH)
    assert first is not None
    assert first.is_admin is True
    async with committed_database.tenant(TenantId(first.tenant_id)) as scope:
        staff = await scope.users.add(email="staff@example.com", password_hash=HASH)
        await scope.commit()
    assert staff.is_admin is False


async def test_second_admin_in_tenant_is_rejected(committed_database: Database) -> None:
    """Т3: в арендаторе один администратор; в разных арендаторах администраторы допустимы."""
    tenant_a = await _tenant(committed_database, "a")
    tenant_b = await _tenant(committed_database, "b")
    async with committed_database.tenant(tenant_a) as scope:
        first = await scope.users.add(email="a1@example.com", password_hash=HASH)
        first.is_admin = True
        await scope.commit()
    async with committed_database.tenant(tenant_b) as scope:
        other = await scope.users.add(email="b1@example.com", password_hash=HASH)
        other.is_admin = True
        await scope.commit()
    async with committed_database.tenant(tenant_a) as scope:
        second = await scope.users.add(email="a2@example.com", password_hash=HASH)
        second.is_admin = True
        with pytest.raises(ConstraintViolationError) as caught:
            await scope.commit()
    assert caught.value.constraint == "uq_app_user_tenant_id_admin"


async def test_delete_member_only_removes_members_of_own_tenant(
    committed_database: Database,
) -> None:
    """Т4."""
    tenant_a = await _tenant(committed_database, "a")
    tenant_b = await _tenant(committed_database, "b")
    async with committed_database.tenant(tenant_a) as scope:
        admin = await scope.users.add(email="admin@example.com", password_hash=HASH)
        admin.is_admin = True
        member = await scope.users.add(email="member@example.com", password_hash=HASH)
        await scope.commit()
    async with committed_database.tenant(tenant_b) as scope:
        foreign = await scope.users.add(email="foreign@example.com", password_hash=HASH)
        await scope.commit()

    async with committed_database.tenant(tenant_a) as scope:
        assert await scope.users.delete_member(admin.id) is False
        assert await scope.users.delete_member(foreign.id) is False
        assert await scope.users.delete_member(member.id) is True
        assert await scope.users.delete_member(member.id) is False
        await scope.commit()

    async with committed_database.tenant(tenant_a) as scope:
        assert [u.email for u in await scope.users.list()] == ["admin@example.com"]
    async with committed_database.tenant(tenant_b) as scope:
        assert await scope.users.get(foreign.id) is not None


async def test_event_keeps_author_email_after_user_deleted(
    committed_database: Database, database_url: str
) -> None:
    """Т5: снимок адреса; удаление автора обнуляет `user_id`, остальное не меняется."""
    tenant_a = await _tenant(committed_database, "a")
    tenant_b = await _tenant(committed_database, "b")
    async with committed_database.tenant(tenant_a) as scope:
        admin = await scope.users.add(email="admin@example.com", password_hash=HASH)
        admin.is_admin = True
        member = await scope.users.add(email="member@example.com", password_hash=HASH)
        await scope.commit()
        event = await scope.owner_events.add(
            kind=OwnerEventKind.LOGIN, user_id=member.id, ip="10.0.0.1"
        )
        await scope.commit()
    assert event.user_email == "member@example.com"
    async with committed_database.tenant(tenant_b) as scope:
        foreign = await scope.users.add(email="foreign@example.com", password_hash=HASH)
        await scope.commit()

    async with committed_database.tenant(tenant_a) as scope:
        assert await scope.users.delete_member(member.id) is True
        await scope.commit()

    rows = await _sql(
        database_url,
        "SELECT user_id, user_email, kind, ip, tenant_id FROM owner_event WHERE id = :i",
        i=event.id,
    )
    assert rows == [(None, "member@example.com", "login", "10.0.0.1", tenant_a)]

    # автор из чужого арендатора: подзапрос даёт NULL, вставка отвергнута, строки нет
    async with committed_database.tenant(tenant_a) as scope:
        with pytest.raises(ConstraintViolationError):
            await scope.owner_events.add(
                kind=OwnerEventKind.LOGIN, user_id=foreign.id, ip="10.0.0.2"
            )
    count = await _sql(database_url, "SELECT count(*) FROM owner_event WHERE ip = '10.0.0.2'")
    assert count == [(0,)]


async def test_user_events_have_no_shop(committed_database: Database) -> None:
    """Т6."""
    tenant = await _tenant(committed_database, "a")
    async with committed_database.tenant(tenant) as scope:
        admin = await scope.users.add(email="admin@example.com", password_hash=HASH)
        await scope.commit()
        added = await scope.owner_events.add(
            kind=OwnerEventKind.USER_ADDED, user_id=admin.id, ip="1.1.1.1", new_value="n@x.kz"
        )
        removed = await scope.owner_events.add(
            kind=OwnerEventKind.USER_REMOVED, user_id=admin.id, ip="1.1.1.1", old_value="n@x.kz"
        )
        await scope.commit()
        assert (added.kind, removed.kind) == ("user_added", "user_removed")
        for kind in (OwnerEventKind.USER_ADDED, OwnerEventKind.USER_REMOVED):
            with pytest.raises(ValueError):
                await scope.owner_events.add(
                    kind=kind, user_id=admin.id, ip="1.1.1.1", shop_id=uuid.uuid4()
                )
