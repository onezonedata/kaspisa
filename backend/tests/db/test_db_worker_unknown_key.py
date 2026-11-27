"""Т13: `SystemScope.unknown_key_spread` — сколько магазинов затронуто неизвестным ключом."""

from __future__ import annotations

import uuid

from db_worker_seed import insert_shop, insert_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kaspisa.db import Database
from kaspisa.security import (
    KeyRing,
    Secret,
    SecretCipher,
    SecretContext,
    SecretField,
)

FOREIGN_KEY_ID = 9


async def _shop_with_blocks(
    connection: AsyncConnection, cipher: SecretCipher, *, foreign: tuple[str, ...] = ()
) -> uuid.UUID:
    """Магазин с настоящими блоками; перечисленные столбцы зашифрованы чужим ключом."""
    tenant = await insert_tenant(connection)
    shop = await insert_shop(connection, tenant)
    foreign_cipher = SecretCipher(KeyRing(FOREIGN_KEY_ID, {FOREIGN_KEY_ID: bytes(32)}))
    fields = {
        "login_enc": SecretField.SHOP_LOGIN,
        "password_enc": SecretField.SHOP_PASSWORD,
        "session_enc": SecretField.SHOP_SESSION,
    }
    for column, field in fields.items():
        encryptor = foreign_cipher if column in foreign else cipher
        blob = encryptor.encrypt(Secret("v"), SecretContext(tenant, shop, field))
        await connection.execute(
            text(f"UPDATE shop SET {column} = :b WHERE id = :id"), {"b": blob, "id": shop}
        )
    return shop


async def test_single_shop_with_unknown_key(
    database: Database, connection: AsyncConnection, cipher: SecretCipher
) -> None:
    broken = await _shop_with_blocks(connection, cipher, foreign=("password_enc",))
    healthy = await _shop_with_blocks(connection, cipher)
    async with database.system() as system:
        assert await system.unknown_key_spread(broken) == 1
        assert await system.unknown_key_spread(healthy) == 0


async def test_shared_unknown_key_counts_all_shops(
    database: Database, connection: AsyncConnection, cipher: SecretCipher
) -> None:
    first = await _shop_with_blocks(connection, cipher, foreign=("login_enc", "session_enc"))
    second = await _shop_with_blocks(connection, cipher, foreign=("session_enc",))
    healthy = await _shop_with_blocks(connection, cipher)
    async with database.system() as system:
        assert await system.unknown_key_spread(first) == 2
        assert await system.unknown_key_spread(second) == 2
        assert await system.unknown_key_spread(healthy) == 0


async def test_different_unknown_keys_are_not_shared(
    database: Database, connection: AsyncConnection, cipher: SecretCipher
) -> None:
    first = await _shop_with_blocks(connection, cipher, foreign=("login_enc",))
    second = await _shop_with_blocks(connection, cipher)
    # у второго магазина блок на другом неизвестном ключе (идентификатор 7)
    blob = bytearray(
        (
            await connection.execute(
                text("SELECT login_enc FROM shop WHERE id = :i"), {"i": second}
            )
        ).scalar_one()
    )
    blob[1] = 7
    await connection.execute(
        text("UPDATE shop SET login_enc = :b WHERE id = :i"), {"b": bytes(blob), "i": second}
    )
    async with database.system() as system:
        assert await system.unknown_key_spread(first) == 1
        assert await system.unknown_key_spread(second) == 1


async def test_null_session_and_missing_shop(
    database: Database, connection: AsyncConnection, cipher: SecretCipher
) -> None:
    shop = await _shop_with_blocks(connection, cipher)
    await connection.execute(
        text(
            "UPDATE shop SET session_enc = NULL, connection_state = 'not_connected', "
            "merchant_uid = NULL WHERE id = :i"
        ),
        {"i": shop},
    )
    async with database.system() as system:
        assert await system.unknown_key_spread(shop) == 0
        assert await system.unknown_key_spread(uuid.uuid4()) == 0


async def test_spread_does_not_expose_block_values(
    database: Database, connection: AsyncConnection, cipher: SecretCipher
) -> None:
    shop = await _shop_with_blocks(connection, cipher, foreign=("login_enc",))
    async with database.system() as system:
        result = await system.unknown_key_spread(shop)
    assert isinstance(result, int)
