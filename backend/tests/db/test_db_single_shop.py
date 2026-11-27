"""Т6 этапа 20: `ShopRepository.lock_owner` держит блокировку строки арендатора."""

from __future__ import annotations

import asyncio

from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from kaspisa.db import Database, OwnerEventKind, TenantId


async def _lock_waiters(url: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            sql = (
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            return int((await conn.execute(text(sql))).scalar_one())
    finally:
        await engine.dispose()


async def test_lock_owner_blocks_second_lock_but_not_inserts(
    committed_database: Database, database_url: str
) -> None:
    db = committed_database
    async with db.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    async with db.tenant(tenant_id) as scope:
        user = await scope.users.add(email="lock@example.kz", password_hash="h")
        await scope.commit()

    second_done = asyncio.Event()

    async def second() -> None:
        async with db.tenant(tenant_id) as scope:
            await scope.shops.lock_owner()
            second_done.set()

    async with db.tenant(tenant_id) as first:
        await first.shops.lock_owner()
        task = asyncio.create_task(second())
        waiting = 0
        for _ in range(100):
            waiting = await _lock_waiters(database_url)
            if waiting:
                break
            await asyncio.sleep(0.05)
        assert waiting == 1, "второй lock_owner не ждёт"
        assert not second_done.is_set()

        # FOR NO KEY UPDATE не конфликтует с FOR KEY SHARE вставки с FK на tenant.
        async with db.tenant(tenant_id) as other:
            await other._session.execute(text("SET LOCAL lock_timeout = '2s'"))
            await other.owner_events.add(kind=OwnerEventKind.LOGIN, user_id=user.id, ip="1.2.3.4")
            await other.commit()

        await first.commit()

    await asyncio.wait_for(task, 5)
    assert second_done.is_set()


async def test_lock_owner_of_one_tenant_does_not_block_another(
    committed_database: Database,
) -> None:
    """Изоляция: блокировка арендатора A не ждёт и не держит арендатора B."""
    db = committed_database
    ids = []
    for name in ("a", "b"):
        async with db.system() as system:
            ids.append(TenantId((await system.create_tenant(name=name)).id))
            await system.commit()
    async with db.tenant(ids[0]) as first:
        await first.shops.lock_owner()
        async with db.tenant(ids[1]) as other:
            await other._session.execute(text("SET LOCAL lock_timeout = '2s'"))
            await other.shops.lock_owner()
