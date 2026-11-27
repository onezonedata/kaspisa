"""ADR 0025, Т1-Т8: таблица настроек Telegram, шифрование токена, ротация, отметки."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from db_seed import SeededTenant, TwoTenants, seed_tenant
from db_testing import (
    alembic_config,
    create_test_database_sync,
    drop_test_database_sync,
    new_test_db_name,
    sqlalchemy_url,
)
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command
from kaspisa.db import (
    Database,
    NotFoundError,
    TenantId,
    UnreadableSecret,
    check_shop_secrets,
    reencrypt_shop_secrets,
)
from kaspisa.db.repositories import NO_SHOP
from kaspisa.db.rotate_secrets import run_command
from kaspisa.db.system import HaltNotice, SecretsReport
from kaspisa.security import (
    DecryptionError,
    KeyRing,
    Secret,
    SecretCipher,
    SecretContext,
    SecretField,
)

T = "123456789:" + "A" * 35
T2 = "987654321:" + "B" * 35


# --- Т1. Миграция -----------------------------------------------------------------------


async def _query(url: str, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return [tuple(row) for row in result.all()] if result.returns_rows else []
    finally:
        await engine.dispose()


def _run(url: str, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    return asyncio.run(_query(url, sql, **params))


_TABLE = "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'telegram_settings'"
_COLUMN = (
    "SELECT 1 FROM information_schema.columns WHERE table_name = 'shop' "
    "AND column_name = 'halt_notified_at'"
)


def test_migration_0009_adds_table_and_marks_old_halts_and_rolls_back() -> None:
    """Т1: остановки до обновления считаются уведомлёнными; откат убирает таблицу и столбец."""
    name = new_test_db_name() + "_m"
    create_test_database_sync(name)
    try:
        url = sqlalchemy_url(name)
        config = alembic_config(url)
        command.upgrade(config, "0008")
        assert not _run(url, _TABLE)
        assert not _run(url, _COLUMN)
        tenant = uuid.uuid4()
        _run(url, "INSERT INTO tenant (id, name) VALUES (:i, 't')", i=tenant)
        halted, free = uuid.uuid4(), uuid.uuid4()
        when = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
        for shop, reason, at in ((halted, "write_errors", when), (free, None, None)):
            _run(
                url,
                "INSERT INTO shop (id, tenant_id, login_enc, password_enc, halt_reason, halted_at) "
                "VALUES (:i, :t, :b, :b, :r, :a)",
                i=shop,
                t=tenant,
                b=b"x",
                r=reason,
                a=at,
            )

        command.upgrade(config, "0009")  # не head: 0010 удаляет засеянные магазины без uid
        assert _run(url, _TABLE)
        rows = dict(_run(url, "SELECT id, halt_notified_at FROM shop"))
        assert rows[halted] == when
        assert rows[free] is None

        before = _run(url, "SELECT id, halt_reason, halted_at FROM shop ORDER BY id")
        command.downgrade(config, "0008")
        assert not _run(url, _TABLE)
        assert not _run(url, _COLUMN)
        assert _run(url, "SELECT id, halt_reason, halted_at FROM shop ORDER BY id") == before
    finally:
        drop_test_database_sync(name)


# --- Т2-Т4. Репозиторий -----------------------------------------------------------------


async def _tenant(database: Database, name: str) -> SeededTenant:
    return await seed_tenant(database, name)


async def _raw_token(connection: AsyncConnection, tenant_id: TenantId) -> bytes:
    row = await connection.execute(
        text("SELECT token_enc FROM telegram_settings WHERE tenant_id = :t"), {"t": tenant_id}
    )
    return bytes(row.scalar_one())


async def test_token_is_encrypted_and_readable(
    database: Database, connection: AsyncConnection
) -> None:
    """Т2: в базе нет токена открытым текстом; repr не показывает токен."""
    a = await _tenant(database, "a")
    async with database.tenant(a.tenant_id) as scope:
        saved = await scope.telegram.save(chat_id="42", token=Secret(T))
        await scope.commit()
        assert T not in repr(saved)
    blob = await _raw_token(connection, a.tenant_id)
    assert T.encode() not in blob
    async with database.tenant(a.tenant_id) as scope:
        target = await scope.telegram.get_target()
        assert target is not None
        assert target.token.reveal() == T
        assert target.chat_id == "42"
        assert T not in repr(target)
        row = await scope.telegram.get()
        assert row is not None
        assert T not in repr(row)


async def test_token_block_is_bound_to_tenant_and_field(
    database: Database, connection: AsyncConnection
) -> None:
    """Т3: блок чужого арендатора и блок поля магазина не расшифровываются как токен."""
    two = TwoTenants(a=await _tenant(database, "a"), b=await _tenant(database, "b"))
    for seed in (two.a, two.b):
        async with database.tenant(seed.tenant_id) as scope:
            await scope.telegram.save(chat_id="42", token=Secret(T))
            await scope.commit()
    await connection.execute(
        text(
            "UPDATE telegram_settings SET token_enc = "
            "(SELECT token_enc FROM telegram_settings WHERE tenant_id = :a) WHERE tenant_id = :b"
        ),
        {"a": two.a.tenant_id, "b": two.b.tenant_id},
    )
    async with database.tenant(two.b.tenant_id) as scope:
        with pytest.raises(DecryptionError):
            await scope.telegram.get_target()
    await connection.execute(
        text(
            "UPDATE telegram_settings SET token_enc = "
            "(SELECT session_enc FROM shop WHERE id = :s) WHERE tenant_id = :a"
        ),
        {"s": two.a.shop_id, "a": two.a.tenant_id},
    )
    async with database.tenant(two.a.tenant_id) as scope:
        with pytest.raises(DecryptionError):
            await scope.telegram.get_target()


async def test_save_keeps_token_and_version_and_delete(database: Database) -> None:
    """Т4: `token=None` оставляет токен; без строки NotFoundError; delete возвращает bool."""
    a = await _tenant(database, "a")
    async with database.tenant(a.tenant_id) as scope:
        with pytest.raises(NotFoundError):
            await scope.telegram.save(chat_id="7", token=None)
        assert await scope.telegram.get() is None
        await scope.telegram.save(chat_id="42", token=Secret(T))
        await scope.commit()
    async with database.system() as system:
        assert await system.mark_version_notified(a.tenant_id, "1.2.0")
        await system.commit()
    async with database.tenant(a.tenant_id) as scope:
        await scope.telegram.save(chat_id="7", token=None)
        await scope.commit()
        target = await scope.telegram.get_target()
        assert target is not None
        assert (target.token.reveal(), target.chat_id) == (T, "7")
        await scope.telegram.save(chat_id="8", token=Secret(T2))
        await scope.commit()
        row = await scope.telegram.get()
        assert row is not None
        assert row.chat_id == "8"
        assert row.notified_version == "1.2.0"
        target = await scope.telegram.get_target()
        assert target is not None
        assert target.token.reveal() == T2
        assert await scope.telegram.delete() is True
        await scope.commit()
        assert await scope.telegram.get() is None
        assert await scope.telegram.get_target() is None
        assert await scope.telegram.delete() is False


# --- Т6. Ротация ------------------------------------------------------------------------


class _Rig:
    def __init__(self, connection: AsyncConnection) -> None:
        key1, key2 = os.urandom(32), os.urandom(32)
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        self.old_db = Database(factory, cipher=SecretCipher(KeyRing(1, {1: key1})))
        self.new_db = Database(factory, cipher=SecretCipher(KeyRing(2, {2: key2, 1: key1})))
        self.connection = connection


@pytest.fixture
def rig(connection: AsyncConnection) -> _Rig:
    return _Rig(connection)


async def _seed_telegram(rig: _Rig) -> TenantId:
    async with rig.old_db.system() as system:
        tenant = await system.create_tenant(name="t")
        await system.commit()
    tenant_id = TenantId(tenant.id)
    async with rig.old_db.tenant(tenant_id) as scope:
        await scope.telegram.save(chat_id="42", token=Secret(T))
        await scope.commit()
    return tenant_id


async def test_rotation_check_and_reencrypt_cover_token(rig: _Rig) -> None:
    """Т6: токен на старом ключе виден проверке и перешифровывается."""
    tenant_id = await _seed_telegram(rig)

    checked = await check_shop_secrets(rig.new_db)
    assert checked.pending == 1
    assert dict(checked.blocks_by_key) == {1: 1}

    report = await reencrypt_shop_secrets(rig.new_db)
    assert report.rotated == 1
    assert dict(report.blocks_by_key) == {2: 1}
    assert (await _raw_token(rig.connection, tenant_id))[1] == 2
    async with rig.new_db.tenant(tenant_id) as scope:
        target = await scope.telegram.get_target()
    assert target is not None
    assert target.token.reveal() == T


async def test_rotation_reports_unreadable_and_unknown_key(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Т6: испорченный блок и блок на ключе 9 попадают в отчёт с адресом токена, не меняясь."""
    tenant_id = await _seed_telegram(rig)
    original = await _raw_token(rig.connection, tenant_id)
    broken = bytearray(original)
    broken[-1] ^= 0x01
    await rig.connection.execute(
        text("UPDATE telegram_settings SET token_enc = :b WHERE tenant_id = :t"),
        {"b": bytes(broken), "t": tenant_id},
    )
    report = await reencrypt_shop_secrets(rig.new_db)
    address = UnreadableSecret(tenant_id, NO_SHOP, SecretField.TELEGRAM_TOKEN)
    assert list(report.unreadable) == [address]
    assert await _raw_token(rig.connection, tenant_id) == bytes(broken)

    foreign = SecretCipher(KeyRing(9, {9: os.urandom(32)})).encrypt(
        Secret(T), SecretContext(tenant_id, NO_SHOP, SecretField.TELEGRAM_TOKEN)
    )
    await rig.connection.execute(
        text("UPDATE telegram_settings SET token_enc = :b WHERE tenant_id = :t"),
        {"b": foreign, "t": tenant_id},
    )
    report = await check_shop_secrets(rig.new_db)
    assert list(report.unknown_key) == [address]
    assert report.unknown_key_ids == [9]
    assert await run_command(rig.new_db, check=True) == 3
    printed = capsys.readouterr().out
    assert f"арендатор {tenant_id}, поле tenant.telegram_token" in printed
    assert "магазин" not in printed.split("нечитаемых блоков")[1]
    assert T not in printed


def test_print_report_for_tenant_block(capsys: pytest.CaptureFixture[str]) -> None:
    """Т6: строка отчёта о токене не называет магазин."""
    from kaspisa.db.rotate_secrets import _print_report

    tenant_id = uuid.uuid4()
    item = UnreadableSecret(tenant_id, NO_SHOP, SecretField.TELEGRAM_TOKEN)
    _print_report(
        SecretsReport(rotated=0, blocks_by_key={1: 1}, pending=0, unreadable=[item]), check=True
    )
    lines = capsys.readouterr().out.splitlines()
    assert f"  арендатор {tenant_id}, поле tenant.telegram_token" in lines


# --- Т7-Т8. Отметки ---------------------------------------------------------------------

WHEN = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


async def test_halt_notices_and_marks(database: Database) -> None:
    """Т7: остановка видна до отметки, отметка условна по `halted_at`."""
    a = await _tenant(database, "a")
    async with database.tenant(a.tenant_id) as scope:
        await scope.shops.halt(a.shop_id, "write_errors", WHEN)
        await scope.commit()
    notice = HaltNotice(a.tenant_id, a.shop_id, a.merchant_uid, "write_errors", WHEN)
    async with database.system() as system:
        assert await system.pending_halt_notices(100) == [notice]
        assert await system.pending_halt_notices(100) == [notice]
        assert await system.mark_halt_notified(a.shop_id, WHEN) is True
        await system.commit()
        assert await system.pending_halt_notices(100) == []
        assert await system.mark_halt_notified(a.shop_id, WHEN) is False

    later = WHEN + timedelta(hours=1)
    async with database.tenant(a.tenant_id) as scope:
        await scope.shops.clear_halt(a.shop_id)
        await scope.shops.halt(a.shop_id, "login_errors", later)
        await scope.commit()
    async with database.system() as system:
        assert await system.pending_halt_notices(100) == [
            HaltNotice(a.tenant_id, a.shop_id, a.merchant_uid, "login_errors", later)
        ]
        assert await system.mark_halt_notified(a.shop_id, WHEN) is False
        assert len(await system.pending_halt_notices(100)) == 1


async def test_halt_mark_with_stale_time_is_rejected_when_never_marked(
    database: Database,
) -> None:
    """Т7: отметка со старым временем не ставится, даже если новая остановка ещё не отмечена."""
    a = await _tenant(database, "a")
    later = WHEN + timedelta(hours=1)
    async with database.tenant(a.tenant_id) as scope:
        await scope.shops.halt(a.shop_id, "write_errors", later)
        await scope.commit()
    async with database.system() as system:
        assert await system.mark_halt_notified(a.shop_id, WHEN) is False
        await system.commit()
        assert len(await system.pending_halt_notices(100)) == 1
        assert await system.mark_halt_notified(a.shop_id, later) is True


async def test_halt_notices_skip_running_shop_and_respect_limit(database: Database) -> None:
    """Т7: неостановленный магазин не возвращается; `limit` берёт самую раннюю остановку."""
    a = await _tenant(database, "a")
    b = await _tenant(database, "b")
    async with database.system() as system:
        assert await system.pending_halt_notices(100) == []
    early, late = WHEN, WHEN + timedelta(minutes=5)
    async with database.tenant(b.tenant_id) as scope:
        await scope.shops.halt(b.shop_id, "write_errors", late)
        await scope.commit()
    async with database.tenant(a.tenant_id) as scope:
        await scope.shops.halt(a.shop_id, "login_errors", early)
        await scope.commit()
    async with database.system() as system:
        notices = await system.pending_halt_notices(1)
        assert [(n.shop_id, n.halted_at) for n in notices] == [(a.shop_id, early)]
        assert len(await system.pending_halt_notices(100)) == 2


async def test_version_marks(database: Database) -> None:
    """Т8: версия отмечается по арендатору; арендатор без настроек не возвращается."""
    a = await _tenant(database, "a")
    b = await _tenant(database, "b")
    c = await _tenant(database, "c")
    for seed in (a, b):
        async with database.tenant(seed.tenant_id) as scope:
            await scope.telegram.save(chat_id="42", token=Secret(T))
            await scope.commit()
    async with database.system() as system:
        assert set(await system.pending_version_tenants("1.2.0")) == {a.tenant_id, b.tenant_id}
        assert await system.mark_version_notified(a.tenant_id, "1.2.0") is True
        await system.commit()
        assert await system.mark_version_notified(a.tenant_id, "1.2.0") is False
        assert set(await system.pending_version_tenants("1.2.0")) == {b.tenant_id}
        assert set(await system.pending_version_tenants("1.3.0")) == {a.tenant_id, b.tenant_id}
        assert c.tenant_id not in await system.pending_version_tenants("1.3.0")
