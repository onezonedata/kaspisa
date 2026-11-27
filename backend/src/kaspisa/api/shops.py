"""Маршруты магазина: список, добавление, карточка, доступы, настройки, остановка, удаление."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from redis.exceptions import RedisError

from kaspisa.api.auth import client_ip
from kaspisa.api.deps import RESEND_INTERVAL_SECONDS, ApiDeps, CurrentPrincipal, Deps
from kaspisa.api.errors import ApiError
from kaspisa.api.notifications import notify_owner
from kaspisa.api.schemas import CatalogSummaryOut, ShopCredentialsIn, ShopOut, ShopSettingsIn
from kaspisa.api.telegram import text_live
from kaspisa.api.views import shop_out
from kaspisa.db import BotMode, OwnerEventKind, PriceWriteStatus, Shop, TenantScope
from kaspisa.kaspi import PRICE_WRITE_HOLD_SECONDS
from kaspisa.security import Secret
from kaspisa.worker.redis_limits import read_dry_run

logger = logging.getLogger("kaspisa.api")
router = APIRouter(prefix="/api/shops")

_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)
_HOLDING = frozenset(
    {
        PriceWriteStatus.SENDING.value,
        PriceWriteStatus.RECEIPT.value,
        PriceWriteStatus.UNKNOWN.value,
    }
)


async def build_shop_out(deps: ApiDeps, shop: Shop) -> ShopOut:
    """`ShopOut` с таймером письма и меткой «нужен вход заново» (из Redis и часов базы)."""
    resend: int | None = None
    restart = False
    if shop.connection_state == "awaiting_code":
        now = deps.clock()
        from_redis = await deps.state.resend_left(shop.id) or 0
        ready_at = shop.connection_changed_at + timedelta(seconds=RESEND_INTERVAL_SECONDS)
        from_db = math.ceil((ready_at - now).total_seconds())
        resend = max(from_redis, from_db, 0)
        restart = await deps.state.restart_marked(shop.id, shop.connection_version)
    return shop_out(
        shop,
        resend_available_in_seconds=resend,
        restart_required=restart,
        catalog_loading=await _catalog_loading(deps, shop),
        server_dry_run=await _worker_flag(deps),
    )


async def _worker_flag(deps: ApiDeps) -> bool | None:
    """Флаг `KASPISA_DRY_RUN`, который опубликовал обработчик; `None` - не отзывается.

    Сбой Redis не ломает ответ: показывается «обработчик не отвечает».
    """
    try:
        return await read_dry_run(deps.redis, deps.worker_prefix)
    except (RedisError, OSError):
        logger.warning("worker_flag_unavailable")
        return None


async def _catalog_loading(deps: ApiDeps, shop: Shop) -> bool:
    """Занята ли блокировка загрузки каталога; только у подключённого магазина.

    Сбой Redis не ломает ответ: показывается «не загружается».
    """
    if shop.connection_state != "connected":
        return False
    try:
        return await deps.sync_lock.held(shop.id)
    except (RedisError, OSError):
        logger.warning("catalog_state_unavailable shop_id=%s", shop.id)
        return False


async def _ensure_no_open_writes(deps: ApiDeps, scope: TenantScope, shop_id: uuid.UUID) -> None:
    """409, если у магазина есть запись цены с действующим запретом (6.2.1 проекта)."""
    now = deps.clock()
    hold = timedelta(seconds=PRICE_WRITE_HOLD_SECONDS)
    rows = await scope.price_writes.due_for_verification(shop_id, _FAR_FUTURE)
    left = [
        math.ceil((row.sent_at + hold - now).total_seconds())
        for row in rows
        if row.status in _HOLDING and now < row.sent_at + hold
    ]
    if left:
        raise ApiError(409, "price_write_open", retry_after_seconds=max(left))


@router.get("")
async def list_shops(principal: CurrentPrincipal, deps: Deps) -> list[ShopOut]:
    async with deps.database.tenant(principal.tenant_id) as scope:
        shops = await scope.shops.list()
    return [await build_shop_out(deps, shop) for shop in shops]


@router.post("", status_code=201)
async def add_shop(body: ShopCredentialsIn, principal: CurrentPrincipal, deps: Deps) -> ShopOut:
    """Один магазин на арендатора (ADR 0029): проверка под блокировкой строки арендатора."""
    async with deps.database.tenant(principal.tenant_id) as scope:
        await scope.shops.lock_owner()
        if await scope.shops.list():
            raise ApiError(409, "shop_limit_reached")
        shop = await scope.shops.add(login=Secret(body.login), password=Secret(body.password))
        await scope.commit()
    return await build_shop_out(deps, shop)


@router.get("/{shop_id}")
async def get_shop(shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps) -> ShopOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.get(shop_id)
    if shop is None:
        raise ApiError(404, "not_found")
    return await build_shop_out(deps, shop)


@router.get("/{shop_id}/catalog-summary")
async def catalog_summary(
    shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps
) -> CatalogSummaryOut:
    """Число товаров и отслеживаемых: счёт в базе, без предела каталога."""
    async with deps.database.tenant(principal.tenant_id) as scope:
        if await scope.shops.get(shop_id) is None:
            raise ApiError(404, "not_found")
        total, tracked = await scope.products.count_for_shop(shop_id)
    return CatalogSummaryOut(total=total, tracked=tracked)


@router.put("/{shop_id}/credentials")
async def change_credentials(
    shop_id: uuid.UUID, body: ShopCredentialsIn, principal: CurrentPrincipal, deps: Deps
) -> ShopOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        await scope.shops.lock_for_write(shop_id)  # NotFoundError - 404
        await _ensure_no_open_writes(deps, scope, shop_id)
        shop = await scope.shops.set_credentials(
            shop_id, login=Secret(body.login), password=Secret(body.password)
        )
        # Прежний блок сессии принадлежит старым доступам: правило ADR 0006 (`previous=None`)
        # действует до первого успешного входа с новыми доступами.
        shop.session_enc = None
        await scope.commit()
    return await build_shop_out(deps, shop)


@router.patch("/{shop_id}/settings")
async def update_settings(
    shop_id: uuid.UUID,
    body: ShopSettingsIn,
    request: Request,
    principal: CurrentPrincipal,
    deps: Deps,
) -> ShopOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.lock_for_write(shop_id)  # NotFoundError - 404
        old_mode = shop.bot_mode
        # Присваиваются только переданные поля: UPDATE затронет только их столбцы.
        for name in body.model_fields_set:
            setattr(shop, name, _plain(getattr(body, name)))
        if shop.bot_mode != BotMode.OFF and shop.city_id is None:
            raise ApiError(422, "validation_error", fields=["body.city_id"])
        if "bot_mode" in body.model_fields_set and shop.bot_mode != old_mode:
            await scope.owner_events.add(
                kind=OwnerEventKind.BOT_MODE_CHANGED,
                user_id=principal.user_id,
                ip=client_ip(request),
                shop_id=shop.id,
                old_value=old_mode,
                new_value=shop.bot_mode,
            )
        await scope.commit()
        if (
            "bot_mode" in body.model_fields_set
            and old_mode != BotMode.LIVE
            and shop.bot_mode == BotMode.LIVE
        ):
            notify_owner(deps, principal.tenant_id, text_live(shop.merchant_uid))
    return await build_shop_out(deps, shop)


def _plain(value: object) -> object:
    return getattr(value, "value", value)


@router.post("/{shop_id}/halt/clear")
async def clear_halt(shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps) -> ShopOut:
    async with deps.database.tenant(principal.tenant_id) as scope:
        shop = await scope.shops.clear_halt(shop_id)  # NotFoundError - 404
        await scope.commit()
    return await build_shop_out(deps, shop)


@router.delete("/{shop_id}", status_code=204)
async def delete_shop(shop_id: uuid.UUID, principal: CurrentPrincipal, deps: Deps) -> Response:
    async with deps.database.tenant(principal.tenant_id) as scope:
        await scope.shops.lock_for_write(shop_id)  # NotFoundError - 404
        await _ensure_no_open_writes(deps, scope, shop_id)
        await scope.shops.delete(shop_id)
        await scope.commit()
    return Response(status_code=204)
