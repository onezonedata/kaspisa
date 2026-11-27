"""Начальные данные тестов: два арендатора с пользователем, магазином, товаром и журналом."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass

from kaspisa.db import Database, TenantId
from kaspisa.db.models import PriceChangeResult
from kaspisa.security import Secret


@dataclass(frozen=True)
class SeededTenant:
    """Арендатор с данными; открытые значения нужны тестам для проверок."""

    tenant_id: TenantId
    user_id: uuid.UUID
    email: str
    password_hash: str
    shop_id: uuid.UUID
    login: str
    password: str
    session: str
    merchant_uid: str
    product_id: int
    sku: str
    price_change_id: int


@dataclass(frozen=True)
class TwoTenants:
    a: SeededTenant
    b: SeededTenant


async def seed_tenant(database: Database, name: str) -> SeededTenant:
    """Арендатор: пользователь, подключённый магазин, товар и строка журнала."""
    email = f"{name}-{secrets.token_hex(4)}@example.kz"
    password_hash = "hash-" + secrets.token_hex(8)
    login = f"login-{secrets.token_hex(6)}"
    password = f"pw-{secrets.token_hex(8)}"
    session = f"sid-{secrets.token_hex(8)}"
    merchant_uid = f"m-{secrets.token_hex(6)}"
    sku = f"sku-{secrets.token_hex(4)}"
    async with database.system() as system:
        tenant = await system.create_tenant(name=name)
        await system.commit()
    tenant_id = TenantId(tenant.id)
    async with database.tenant(tenant_id) as scope:
        user = await scope.users.add(email=email, password_hash=password_hash)
        shop = await scope.shops.add(login=Secret(login), password=Secret(password))
        shop = await scope.shops.mark_connected(
            shop.id,
            merchant_uid=merchant_uid,
            session_state=Secret(session),
            expected_version=shop.connection_version,
        )
        product = await scope.products.add(
            shop_id=shop.id, sku=sku, master_sku="master", title="Товар"
        )
        change = await scope.price_changes.add(
            product_id=product.id,
            old_price=1000,
            new_price=990,
            reason="undercut_first",
            result=PriceChangeResult.APPLIED,
        )
        await scope.commit()
        return SeededTenant(
            tenant_id=tenant_id,
            user_id=user.id,
            email=email,
            password_hash=password_hash,
            shop_id=shop.id,
            login=login,
            password=password,
            session=session,
            merchant_uid=merchant_uid,
            product_id=product.id,
            sku=sku,
            price_change_id=change.id,
        )
