"""Помощники тестов записи цены: товар, решение, контекст слайса, строки журнала."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from worker_testing import (
    NOW,
    FakeClock,
    FakeKaspiClient,
    Rig,
    SeededShop,
    execute,
    listed_product,
    make_worker_ctx,
    open_slice_ctx,
    seed_shop,
)

from kaspisa.db import Database, Product
from kaspisa.pricing import Decision, Offer, Reason


def decision(new_price: int = 94, competitor_price: int = 95) -> Decision:
    return Decision(
        new_price=new_price,
        reason=Reason.UNDERCUT_FIRST,
        target_price=new_price,
        competitor=Offer(merchant_id="other", price=competitor_price),
    )


class Prepared:
    """Магазин с одним товаром, заглушка клиента и контекст слайса."""

    def __init__(
        self,
        rig: Rig,
        database: Database,
        shop: SeededShop,
        client: FakeKaspiClient,
        clock: FakeClock,
        ctx: Any,
        sctx: Any,
    ) -> None:
        self.rig = rig
        self.database = database
        self.shop = shop
        self.client = client
        self.clock = clock
        self.ctx = ctx
        self.sctx = sctx

    @property
    def sku(self) -> str:
        return self.shop.skus[0]

    @property
    def product_id(self) -> int:
        return self.shop.product_ids[0]

    async def product(self) -> Product:
        async with self.database.tenant(self.shop.tenant_id) as scope:
            product = await scope.products.get(self.product_id)
        assert product is not None
        return product

    def listing(self, price: int = 100) -> Any:
        return listed_product(self.sku, self.shop.merchant_uid, price)

    async def write(self, new_price: int = 94, current: int = 100, **kw: Any) -> Any:
        """Прямой вызов записи: на время вызова аренда у обработчика, если магазин свободен."""
        from kaspisa.worker.writer import write_price

        product = kw.pop("product", None) or await self.product()
        async with self.rig.engine.begin() as conn:
            result = await conn.execute(
                text(
                    "UPDATE shop SET claimed_by=:w, claimed_until=:u "
                    "WHERE id=:i AND claimed_by IS NULL RETURNING id"
                ),
                {
                    "w": self.ctx.config.worker_id,
                    "u": NOW + timedelta(days=3650),
                    "i": self.shop.shop_id,
                },
            )
            taken = result.first() is not None
        try:
            return await write_price(
                self.sctx,
                product,
                kw.pop("decision", None) or decision(new_price),
                current,
            )
        finally:
            if taken:
                await execute(
                    self.rig,
                    "UPDATE shop SET claimed_by=NULL, claimed_until=NULL "
                    "WHERE id=:i AND claimed_by=:w",
                    i=self.shop.shop_id,
                    w=self.ctx.config.worker_id,
                )


async def prepare(
    rig: Rig,
    redis: Redis,
    prefix: str,
    *,
    dry_run: bool = False,
    clock: FakeClock | None = None,
    products: int = 1,
    database: Database | None = None,
    shop: SeededShop | None = None,
    worker_id: str = "w1",
    client: FakeKaspiClient | None = None,
) -> Prepared:
    clock = clock or FakeClock()
    database = database or rig.new_database()
    shop = shop or await seed_shop(rig, database, products=products)
    client = client or FakeKaspiClient()
    client.merchant_uid = shop.merchant_uid
    ctx = make_worker_ctx(
        database, client, redis, prefix, clock, dry_run=dry_run, worker_id=worker_id
    )
    sctx = await open_slice_ctx(ctx, shop)
    return Prepared(rig, database, shop, client, clock, ctx, sctx)


async def add_sent_rows(
    rig: Rig,
    shop: SeededShop,
    count: int,
    created_at: datetime,
    *,
    product_id: int | None = None,
    result: str = "unconfirmed",
) -> None:
    await execute(
        rig,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, created_at, old_price, "
        "new_price, reason, result) SELECT :t, :s, :p, :c, 100, 90, 'x', :r "
        "FROM generate_series(1, :n)",
        t=shop.tenant_id,
        s=shop.shop_id,
        p=product_id or shop.product_ids[0],
        c=created_at,
        r=result,
        n=count,
    )


def new_id() -> uuid.UUID:
    return uuid.uuid4()


async def hold_lease(rig: Rig, shop: SeededShop, worker_id: str, until: datetime) -> None:
    """Аренда магазина без захвата (для прямых вызовов подтверждения)."""
    await execute(
        rig,
        "UPDATE shop SET claimed_by=:w, claimed_until=:u WHERE id=:i",
        w=worker_id,
        u=until,
        i=shop.shop_id,
    )


async def add_open_writes(
    rig: Rig,
    shop: SeededShop,
    product_ids: list[int],
    snapshot: dict[str, Any],
    *,
    sent_at: datetime,
    next_verify_at: datetime,
) -> None:
    """Открытые записи (`receipt`) для товаров: журнал `unconfirmed` и строка запрета."""
    import json

    from sqlalchemy import text

    async with rig.engine.begin() as conn:
        for product_id in product_ids:
            change = await conn.execute(
                text(
                    "INSERT INTO price_change (tenant_id, shop_id, product_id, created_at, "
                    "old_price, new_price, reason, result) VALUES (:t, :s, :p, :c, 100, 94, "
                    "'undercut_first', 'unconfirmed') RETURNING id"
                ),
                {"t": shop.tenant_id, "s": shop.shop_id, "p": product_id, "c": sent_at},
            )
            await conn.execute(
                text(
                    "INSERT INTO price_write (product_id, tenant_id, shop_id, sku, snapshot, "
                    "expected_prices, sent_at, status, next_verify_at, price_change_id) VALUES "
                    "(:p, :t, :s, 'sku', CAST(:snap AS jsonb), CAST(:exp AS jsonb), :sent, "
                    "'receipt', :nv, :c)"
                ),
                {
                    "p": product_id,
                    "t": shop.tenant_id,
                    "s": shop.shop_id,
                    "snap": json.dumps(snapshot),
                    "exp": json.dumps({"750000000": 94}),
                    "sent": sent_at,
                    "nv": next_verify_at,
                    "c": change.scalar_one(),
                },
            )
