"""Правки по ревью этапа 4: autoflush, удаление магазина, ключ вне связки."""

from __future__ import annotations

import os
import traceback
import uuid
from collections.abc import Awaitable, Callable

import pytest
from db_seed import seed_tenant

from kaspisa.db import (
    ConstraintViolationError,
    Database,
    EmailTakenError,
    PriceChangeResult,
    ShopRepository,
)
from kaspisa.security import Secret


def _chain_text(exc: BaseException) -> str:
    """Текст исключения и всей цепочки причин, как его увидел бы лог."""
    return "".join(traceback.format_exception(exc)) + repr(exc.__cause__) + repr(exc.__context__)


async def test_invalid_assignment_then_query_is_guarded_for_shop(
    committed_database: Database,
) -> None:
    """Б1: присваивание, запрос репозитория, commit: ошибка пакета без DETAIL строки."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        assert shop is not None
        shop.default_step = 0  # нарушает CHECK
        with pytest.raises(ConstraintViolationError) as info:
            await scope.shops.list()
            await scope.commit()
    rendered = _chain_text(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    for forbidden in ("Failing row", seed.merchant_uid, "\\x"):
        assert forbidden not in rendered


async def test_invalid_assignment_then_query_is_guarded_for_user(
    committed_database: Database,
) -> None:
    """Б1: хеш пароля не попадает в цепочку исключений."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        user = await scope.users.get(seed.user_id)
        assert user is not None
        user.email = seed.email.upper()  # нарушает CHECK нижнего регистра
        with pytest.raises(ConstraintViolationError) as info:
            await scope.users.list()
            await scope.commit()
    rendered = _chain_text(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    for forbidden in ("Failing row", seed.password_hash, seed.email):
        assert forbidden not in rendered


async def test_delete_shop_keeps_changes_to_other_objects(database: Database) -> None:
    """Удаление магазина не отсоединяет посторонние объекты сессии."""
    seed = await seed_tenant(database, "a")
    async with database.tenant(seed.tenant_id) as scope:
        other = await scope.shops.add(
            login=Secret("l-" + os.urandom(4).hex()), password=Secret("p")
        )
        other_id = other.id
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        other_shop = await scope.shops.get(other_id)
        assert other_shop is not None
        await scope.shops.delete(seed.shop_id)
        other_shop.bot_mode = "live"
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        again = await scope.shops.get(other_id)
        assert again is not None
        assert again.bot_mode == "live"
        assert await scope.shops.get(seed.shop_id) is None


async def test_delete_shop_keeps_changes_to_products_of_other_shop(database: Database) -> None:
    """Т41: товар другого магазина остаётся в сессии, его правка сохраняется."""
    seed = await seed_tenant(database, "a")
    async with database.tenant(seed.tenant_id) as scope:
        other = await scope.shops.add(
            login=Secret("l-" + os.urandom(4).hex()), password=Secret("p")
        )
        product = await scope.products.add(
            shop_id=other.id, sku="other-sku", master_sku="m", title="старое"
        )
        other_id, product_id = other.id, product.id
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        other_product = await scope.products.get(product_id)
        mine = await scope.products.get(seed.product_id)
        assert other_product is not None
        assert mine is not None
        await scope.shops.delete(seed.shop_id)
        other_product.title = "новое"
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        again = await scope.products.get(product_id)
        assert again is not None
        assert again.title == "новое"
        assert again.shop_id == other_id
        assert await scope.products.get(seed.product_id) is None


async def test_delete_shop_does_not_resurrect_deleted_objects(database: Database) -> None:
    """Объекты удалённого магазина, загруженные раньше, не записываются обратно при commit."""
    seed = await seed_tenant(database, "a")
    async with database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        product = await scope.products.get(seed.product_id)
        assert shop is not None
        assert product is not None
        await scope.shops.delete(seed.shop_id)
        shop.bot_mode = "live"
        product.title = "после удаления"
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        assert await scope.shops.get(seed.shop_id) is None
        assert await scope.products.get(seed.product_id) is None


async def test_state_change_keeps_unsaved_assignment_of_same_shop(
    committed_database: Database,
) -> None:
    """Н1: mark_* не стирает несохранённое присваивание того же магазина."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        assert shop is not None
        shop.bot_mode = "live"
        await scope.shops.mark_reauth_required(
            seed.shop_id, error="e", expected_version=shop.connection_version
        )
        await scope.commit()
    async with committed_database.tenant(seed.tenant_id) as scope:
        again = await scope.shops.get(seed.shop_id)
        assert again is not None
        assert again.bot_mode == "live"
        assert again.connection_state == "reauth_required"


async def test_state_change_wraps_constraint_error_from_pending_flush(
    committed_database: Database,
) -> None:
    """Н1: ошибка ограничения при flush перед UPDATE выходит обёрнутой, без DETAIL."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        assert shop is not None
        version = shop.connection_version
        shop.default_step = 0  # нарушает CHECK
        with pytest.raises(ConstraintViolationError) as info:
            await scope.shops.mark_reauth_required(
                seed.shop_id, error="e", expected_version=version
            )
    rendered = _chain_text(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    for forbidden in ("Failing row", seed.merchant_uid, "\\x"):
        assert forbidden not in rendered


async def test_delete_shop_after_caught_constraint_error(committed_database: Database) -> None:
    """Н2: delete после перехваченной ошибки ограничения не падает с MissingGreenlet."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        other = await scope.shops.add(
            login=Secret("l-" + os.urandom(4).hex()), password=Secret("p")
        )
        other_id = other.id
        await scope.commit()
    async with committed_database.tenant(seed.tenant_id) as scope:
        loaded = await scope.shops.get(other_id)  # объект держим в сессии
        assert loaded is not None
        with pytest.raises(EmailTakenError):
            await scope.users.add(email=seed.email, password_hash=seed.password_hash)
        await scope.shops.delete(seed.shop_id)
        await scope.commit()
    async with committed_database.tenant(seed.tenant_id) as scope:
        assert await scope.shops.get(seed.shop_id) is None
        assert await scope.shops.get(other_id) is not None


_FLUSH_CALLS: dict[str, Callable[[ShopRepository, uuid.UUID, int], Awaitable[object]]] = {
    "set_credentials": lambda r, i, v: r.set_credentials(
        i, login=Secret("l"), password=Secret("p")
    ),
    "save_session": lambda r, i, v: r.save_session(i, Secret("s"), expected_version=v),
    "mark_awaiting_code": lambda r, i, v: r.mark_awaiting_code(i, Secret("s"), expected_version=v),
    "mark_connected": lambda r, i, v: r.mark_connected(
        i, merchant_uid="m", session_state=Secret("s"), expected_version=v
    ),
    "mark_reauth_required": lambda r, i, v: r.mark_reauth_required(
        i, error="e", expected_version=v
    ),
}


@pytest.mark.parametrize("method", sorted(_FLUSH_CALLS))
async def test_every_shop_update_keeps_unsaved_assignment(
    committed_database: Database, method: str
) -> None:
    """Н1: ни один метод изменения магазина не стирает несохранённое присваивание."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        assert shop is not None
        shop.default_step = 7
        await _FLUSH_CALLS[method](scope.shops, seed.shop_id, shop.connection_version)
        await scope.commit()
    async with committed_database.tenant(seed.tenant_id) as scope:
        again = await scope.shops.get(seed.shop_id)
        assert again is not None
        assert again.default_step == 7


@pytest.mark.parametrize("method", sorted(_FLUSH_CALLS))
async def test_every_shop_update_wraps_pending_constraint_error(
    committed_database: Database, method: str
) -> None:
    """Н1: ошибка ограничения из-за ожидающего flush выходит обёрнутой во всех методах."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        assert shop is not None
        version = shop.connection_version
        shop.default_step = 0  # нарушает CHECK
        with pytest.raises(ConstraintViolationError) as info:
            await _FLUSH_CALLS[method](scope.shops, seed.shop_id, version)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_delete_shop_keeps_price_changes_of_other_shop(database: Database) -> None:
    """Запись журнала другого магазина остаётся в сессии, её правка сохраняется."""
    seed = await seed_tenant(database, "a")
    async with database.tenant(seed.tenant_id) as scope:
        other = await scope.shops.add(
            login=Secret("l-" + os.urandom(4).hex()), password=Secret("p")
        )
        product = await scope.products.add(
            shop_id=other.id, sku="other-sku", master_sku="m", title="т"
        )
        change = await scope.price_changes.add(
            product_id=product.id,
            old_price=100,
            new_price=90,
            reason="old",
            result=PriceChangeResult.APPLIED,
        )
        other_id, change_id = other.id, change.id
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        loaded = await scope.price_changes.list_for_shop(other_id, limit=10)
        mine = await scope.price_changes.list_for_shop(seed.shop_id, limit=10)
        assert [c.id for c in loaded] == [change_id]
        assert len(mine) == 1
        await scope.shops.delete(seed.shop_id)
        loaded[0].reason = "new"
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        again = await scope.price_changes.list_for_shop(other_id, limit=10)
        assert [c.reason for c in again] == ["new"]
        assert await scope.price_changes.list_for_shop(seed.shop_id, limit=10) == []


async def test_delete_shop_does_not_resurrect_price_changes(database: Database) -> None:
    """Загруженная раньше запись журнала удалённого магазина не записывается обратно."""
    seed = await seed_tenant(database, "a")
    async with database.tenant(seed.tenant_id) as scope:
        mine = await scope.price_changes.list_for_shop(seed.shop_id, limit=10)
        assert len(mine) == 1
        await scope.shops.delete(seed.shop_id)
        mine[0].reason = "после удаления"
        await scope.commit()
    async with database.tenant(seed.tenant_id) as scope:
        assert await scope.price_changes.list_for_shop(seed.shop_id, limit=10) == []


async def test_delete_own_shop_after_caught_constraint_error(committed_database: Database) -> None:
    """Н2: удаляемый магазин, устаревший после отката, не воскресает и не ломает commit."""
    seed = await seed_tenant(committed_database, "a")
    async with committed_database.tenant(seed.tenant_id) as scope:
        shop = await scope.shops.get(seed.shop_id)
        product = await scope.products.get(seed.product_id)
        assert shop is not None
        assert product is not None
        with pytest.raises(EmailTakenError):
            await scope.users.add(email=seed.email, password_hash=seed.password_hash)
        await scope.shops.delete(seed.shop_id)
        await scope.commit()
    async with committed_database.tenant(seed.tenant_id) as scope:
        assert await scope.shops.get(seed.shop_id) is None
        assert await scope.products.get(seed.product_id) is None
