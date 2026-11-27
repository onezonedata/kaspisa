"""Т11–Т15: секреты магазина в базе не читаются без ключа и не видны в repr и ошибках."""

from __future__ import annotations

import os
import traceback
import uuid

import pytest
from db_seed import SeededTenant, TwoTenants
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import (
    ConstraintViolationError,
    Database,
    EmailTakenError,
    ShopAlreadyConnectedError,
)
from kaspisa.security import (
    DecryptionError,
    KeyRing,
    Secret,
    SecretCipher,
    SecretContext,
    SecretField,
    UnknownKeyError,
)


async def _blocks(connection: AsyncConnection, shop_id: uuid.UUID) -> tuple[bytes, bytes, bytes]:
    row = (
        await connection.execute(
            text("SELECT login_enc, password_enc, session_enc FROM shop WHERE id = :i"),
            {"i": shop_id},
        )
    ).one()
    return bytes(row[0]), bytes(row[1]), bytes(row[2])


def _needles(seed: SeededTenant) -> list[bytes]:
    return [v.encode() for v in (seed.login, seed.password, seed.session)]


async def test_secrets_not_in_database(
    connection: AsyncConnection, two_tenants: TwoTenants
) -> None:
    """Т11: ни в блоках, ни в текстовом виде всей строки нет открытых значений."""
    seed = two_tenants.a
    blocks = await _blocks(connection, seed.shop_id)
    row_text = (
        await connection.execute(
            text("SELECT shop::text FROM shop shop WHERE id = :i"), {"i": seed.shop_id}
        )
    ).scalar_one()
    dumps = [*blocks, row_text.encode()]
    for needle in _needles(seed):
        for dump in dumps:
            assert needle not in dump


async def test_blocks_unreadable_without_key(
    connection: AsyncConnection, two_tenants: TwoTenants, cipher: SecretCipher
) -> None:
    """Т12: чужой ключ и связка без ключа не читают блоки; верный ключ читает."""
    seed = two_tenants.a
    login_b, password_b, session_b = await _blocks(connection, seed.shop_id)
    fields = {
        SecretField.SHOP_LOGIN: (login_b, seed.login),
        SecretField.SHOP_PASSWORD: (password_b, seed.password),
        SecretField.SHOP_SESSION: (session_b, seed.session),
    }
    wrong = SecretCipher(KeyRing(1, {1: os.urandom(32)}))
    without = SecretCipher(KeyRing(2, {2: os.urandom(32)}))
    for field, (blob, plain) in fields.items():
        context = SecretContext(seed.tenant_id, seed.shop_id, field)
        with pytest.raises(DecryptionError):
            wrong.decrypt(blob, context)
        with pytest.raises(UnknownKeyError):
            without.decrypt(blob, context)
        assert cipher.decrypt(blob, context).reveal() == plain


async def test_repository_returns_original_values(
    database: Database, two_tenants: TwoTenants
) -> None:
    seed = two_tenants.a
    async with database.tenant(seed.tenant_id) as scope:
        creds = await scope.shops.get_credentials(seed.shop_id)
        session = await scope.shops.get_session(seed.shop_id)
    assert (creds.login.reveal(), creds.password.reveal()) == (seed.login, seed.password)
    assert session is not None
    assert session.reveal() == seed.session


async def test_repr_hides_secrets(database: Database, two_tenants: TwoTenants) -> None:
    """Т13: repr Shop, ShopCredentials, User не содержит секретов."""
    seed = two_tenants.a
    async with database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        user = await scope.users.get(seed.user_id)
        creds = await scope.shops.get_credentials(seed.shop_id)
        session = await scope.shops.get_session(seed.shop_id)
        assert shop is not None
        assert user is not None
        texts = [repr(shop), str(shop), repr(user), repr(creds), repr(session), f"{creds}"]
    forbidden = [seed.login, seed.password, seed.session, seed.password_hash, seed.email]
    for rendered in texts:
        for value in forbidden:
            assert value not in rendered
    assert "Secret('***')" in repr(creds)
    for blob in (shop.login_enc, shop.password_enc, shop.session_enc):
        assert blob is not None
        assert blob.hex() not in repr(shop)
        assert repr(blob) not in repr(shop)
    assert "_enc" not in repr(shop)


async def test_foreign_blocks_not_decryptable(
    database: Database, connection: AsyncConnection, two_tenants: TwoTenants
) -> None:
    """Т14: блок, перенесённый в другой магазин или поле, не расшифровывается."""
    a, b = two_tenants.a, two_tenants.b
    login_a, password_a, _ = await _blocks(connection, a.shop_id)
    await connection.execute(
        text("UPDATE shop SET login_enc = :blob WHERE id = :i"),
        {"blob": login_a, "i": b.shop_id},
    )
    async with database.tenant(b.tenant_id) as scope:
        with pytest.raises(DecryptionError):
            await scope.shops.get_credentials(b.shop_id)

    async with database.tenant(a.tenant_id) as scope:
        other = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        await scope.commit()
    await connection.execute(
        text("UPDATE shop SET login_enc = :blob WHERE id = :i"),
        {"blob": login_a, "i": other.id},
    )
    async with database.tenant(a.tenant_id) as scope:
        with pytest.raises(DecryptionError):
            await scope.shops.get_credentials(other.id)

    await connection.execute(
        text("UPDATE shop SET login_enc = :blob WHERE id = :i"),
        {"blob": password_a, "i": a.shop_id},
    )
    async with database.tenant(a.tenant_id) as scope:
        with pytest.raises(DecryptionError):
            await scope.shops.get_credentials(a.shop_id)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def _assert_clean(exc: BaseException, forbidden: list[str]) -> None:
    assert exc.__cause__ is None
    assert exc.__context__ is None
    rendered = [str(e) + repr(e) for e in _chain(exc)]
    rendered.append("".join(traceback.format_exception(exc)))
    for text_ in rendered:
        for value in forbidden:
            assert value not in text_


async def test_constraint_errors_carry_no_values(
    database: Database, two_tenants: TwoTenants
) -> None:
    """Т15: ошибки ограничений называют ограничение и не несут значений строки."""
    a, b = two_tenants.a, two_tenants.b
    blocks_hex: list[str] = []

    # UNIQUE по email
    async with database.tenant(b.tenant_id) as scope:
        with pytest.raises(EmailTakenError) as email_info:
            await scope.users.add(email=a.email, password_hash=b.password_hash)
    assert email_info.value.constraint == "uq_app_user_email"
    _assert_clean(email_info.value, [a.email, b.password_hash])

    # CHECK на app_user: e-mail с заглавной буквой присваиванием
    async with database.tenant(b.tenant_id) as scope:
        user = await scope.users.get(b.user_id)
        assert user is not None
        upper = "Upper-" + b.email
        user.email = upper
        with pytest.raises(ConstraintViolationError) as check_user:
            await scope.commit()
    assert check_user.value.constraint == "ck_app_user_email_lowercase"
    _assert_clean(check_user.value, [upper, b.password_hash])

    # CHECK на shop: неизвестная стратегия
    async with database.tenant(b.tenant_id) as scope:
        shop = await scope.shops.get(b.shop_id)
        assert shop is not None
        blocks_hex = [shop.login_enc.hex(), shop.password_enc.hex()]
        shop.strategy = "strategy-" + b.merchant_uid
        with pytest.raises(ConstraintViolationError) as check_shop:
            await scope.commit()
    assert check_shop.value.constraint == "ck_shop_strategy"
    _assert_clean(check_shop.value, [b.merchant_uid, b.login, *blocks_hex])

    # UNIQUE по merchant_uid
    async with database.tenant(b.tenant_id) as scope:
        shop = await scope.shops.add(login=Secret("l"), password=Secret("p"))
        with pytest.raises(ShopAlreadyConnectedError) as merchant_info:
            await scope.shops.mark_connected(
                shop.id,
                merchant_uid=a.merchant_uid,
                session_state=Secret("sess-" + a.session),
                expected_version=shop.connection_version,
            )
    assert merchant_info.value.constraint == "uq_shop_merchant_uid"
    _assert_clean(merchant_info.value, [a.merchant_uid, a.session, "sess-" + a.session])
