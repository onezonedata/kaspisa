"""Т1 этапа 19: миграция 0011 -> 0012 на данных и откат (ADR 0039)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from db_worker_seed import insert_tenant
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from alembic import command


@contextmanager
def _database_at_0011() -> Iterator[tuple[str, Any]]:
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0011")
        yield url, config
    finally:
        drop_test_database_sync(name)


def _run(url: str, work: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
    async def go() -> Any:
        engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(go())


async def _user(conn: AsyncConnection, tenant: uuid.UUID, email: str, created: str) -> uuid.UUID:
    user = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO app_user (id, tenant_id, email, password_hash, created_at) "
            "VALUES (:u, :t, :e, 'h', :c)"
        ),
        {"u": user, "t": tenant, "e": email, "c": datetime.fromisoformat(created)},
    )
    return user


async def _event(conn: AsyncConnection, tenant: uuid.UUID, user: uuid.UUID) -> None:
    await conn.execute(
        text(
            "INSERT INTO owner_event (tenant_id, user_id, kind, ip) "
            "VALUES (:t, :u, 'login', '1.1.1.1')"
        ),
        {"t": tenant, "u": user},
    )


async def _fk_action(conn: AsyncConnection) -> str:
    row = await conn.execute(
        text(
            "SELECT confdeltype::text FROM pg_constraint "
            "WHERE conname = 'fk_owner_event_user_id_app_user'"
        )
    )
    return str(row.scalar_one())


async def _columns(conn: AsyncConnection, table: str) -> dict[str, str]:
    rows = await conn.execute(
        text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = current_schema()"
        ),
        {"t": table},
    )
    return {row[0]: row[1] for row in rows}


def test_upgrade_assigns_first_user_of_each_tenant_as_admin(
    capfd: pytest.CaptureFixture[str],
) -> None:
    with _database_at_0011() as (url, config):

        async def seed(conn: AsyncConnection) -> dict[str, uuid.UUID]:
            tenant_a = await insert_tenant(conn)
            tenant_b = await insert_tenant(conn)
            tenant_c = await insert_tenant(conn)
            u1 = await _user(conn, tenant_a, "u1@example.com", "2026-01-01 10:00:00+00")
            u2 = await _user(conn, tenant_a, "u2@example.com", "2026-02-01 10:00:00+00")
            b1 = await _user(conn, tenant_b, "b1@example.com", "2026-03-01 10:00:00+00")
            c1 = await _user(conn, tenant_c, "c1@example.com", "2026-01-01 10:00:00+00")
            c2 = await _user(conn, tenant_c, "c2@example.com", "2026-01-01 10:00:00+00")
            for tenant, user in ((tenant_a, u1), (tenant_a, u2), (tenant_b, b1), (tenant_c, c2)):
                await _event(conn, tenant, user)
            return {"u1": u1, "u2": u2, "b1": b1, "c1": c1, "c2": c2}

        users = _run(url, seed)
        capfd.readouterr()
        command.upgrade(config, "0012")
        # Логирование alembic не настроено (у логгера `alembic` стоит NullHandler), поэтому
        # адреса администраторов печатаются в stderr: их видно в `docker compose logs migrate`.
        shown = [
            line
            for line in capfd.readouterr().err.splitlines()
            if line.startswith("администратор арендатора")
        ]
        assert len(shown) == 3
        for email in ("u1@example.com", "b1@example.com"):
            assert any(email in line for line in shown)

        async def state(conn: AsyncConnection) -> Any:
            admins = {
                row[0] for row in await conn.execute(text("SELECT id FROM app_user WHERE is_admin"))
            }
            events = list(
                await conn.execute(
                    text(
                        "SELECT e.user_email, u.email FROM owner_event e "
                        "JOIN app_user u ON u.id = e.user_id"
                    )
                )
            )
            total = (await conn.execute(text("SELECT count(*) FROM owner_event"))).scalar_one()
            return admins, events, total, await _columns(conn, "owner_event")

        admins, events, total, columns = _run(url, state)
        first_of_c = min(users["c1"], users["c2"])
        assert admins == {users["u1"], users["b1"], first_of_c}
        assert total == 4
        assert len(events) == 4
        assert all(snapshot == email for snapshot, email in events)
        assert columns["user_id"] == "YES"
        assert columns["user_email"] == "NO"
        assert _run(url, _fk_action) == "n"


def test_downgrade_drops_new_data_and_restores_schema() -> None:
    with _database_at_0011() as (url, config):

        async def seed(conn: AsyncConnection) -> uuid.UUID:
            tenant = await insert_tenant(conn)
            user = await _user(conn, tenant, "u1@example.com", "2026-01-01 10:00:00+00")
            await _event(conn, tenant, user)
            return tenant

        tenant = _run(url, seed)
        command.upgrade(config, "0012")

        async def add_new_rows(conn: AsyncConnection) -> None:
            await conn.execute(
                text(
                    "INSERT INTO owner_event (tenant_id, user_id, user_email, kind, ip) VALUES "
                    "(:t, NULL, 'gone@example.com', 'login', '2.2.2.2'), "
                    "(:t, (SELECT id FROM app_user LIMIT 1), 'u1@example.com', "
                    "'user_added', '3.3.3.3')"
                ),
                {"t": tenant},
            )

        _run(url, add_new_rows)
        command.downgrade(config, "0011")

        async def state(conn: AsyncConnection) -> Any:
            ips = [row[0] for row in await conn.execute(text("SELECT ip FROM owner_event"))]
            return (
                ips,
                await _columns(conn, "app_user"),
                await _columns(conn, "owner_event"),
                await _fk_action(conn),
            )

        ips, user_columns, event_columns, action = _run(url, state)
        assert ips == ["1.1.1.1"]
        assert "is_admin" not in user_columns
        assert "user_email" not in event_columns
        assert event_columns["user_id"] == "NO"
        assert action == "c"
        command.upgrade(config, "0012")
