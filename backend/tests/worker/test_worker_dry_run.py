"""Т31: режим «без записи»."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import pytest
from redis.asyncio import Redis
from worker_testing import (
    FakeKaspiClient,
    Rig,
    fetch,
    journal,
    redis_client,
    redis_prefix,
    rig,
    run_one_slice,
    worker_cipher,
    worker_database_url,
)
from writer_testing import prepare


async def test_dry_run_journals_decision_without_writing(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    client = p.client
    await run_one_slice(p.ctx, p.database)
    assert client.methods() == ["find_product", "get_offers"]
    assert await journal(rig, p.shop.shop_id) == [(100, 94, "undercut_first", "dry_run", None)]
    assert await fetch(rig, "SELECT 1 FROM price_write") == []
    p.clock.advance(61)
    await run_one_slice(p.ctx, p.database)
    assert len(await journal(rig, p.shop.shop_id)) == 1  # то же решение не дублируется
    client.competitor_price = 90
    p.clock.advance(61)
    await run_one_slice(p.ctx, p.database)
    assert await journal(rig, p.shop.shop_id) == [
        (100, 94, "undercut_first", "dry_run", None),
        (100, 89, "undercut_first", "dry_run", None),
    ]
    assert "set_price" not in client.methods() and "get_product" not in client.methods()


def test_dry_run_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from kaspisa.config import Settings

    monkeypatch.delenv("KASPISA_DRY_RUN", raising=False)
    assert Settings(_env_file=None).dry_run is True
