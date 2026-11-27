"""Т64, Т65: остановка `Worker.run` по событию и SIGTERM, точка входа процесса."""

# ruff: noqa: F401, F811  (фикстуры импортируются из worker_testing; conftest.py здесь нельзя)

from __future__ import annotations

import asyncio
import importlib
import os
import signal
import sys
from typing import Any

import pytest
from redis.asyncio import Redis
from worker_testing import (
    NOW,
    Rig,
    fetch,
    redis_client,
    redis_port,
    redis_prefix,
    rig,
    shop_row,
    worker_cipher,
    worker_database_url,
)
from writer_testing import Prepared, prepare

from kaspisa.config import Settings
from kaspisa.worker import runner
from kaspisa.worker.constants import SHUTDOWN_GRACE_SECONDS, WORKER_SLOTS
from kaspisa.worker.runner import Worker


async def _wait_for(client: Any, method: str) -> None:
    async with asyncio.timeout(10):
        while method not in client.methods():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def _started(rig: Rig, redis: Redis, prefix: str) -> tuple[Prepared, Worker, asyncio.Event]:
    p = await prepare(rig, redis, prefix, products=3)
    p.client.set_price_gate = asyncio.Event()
    return p, Worker(p.ctx, idle_sleep=0.05), asyncio.Event()


# --- Т64 ---


async def test_stop_during_a_slice_finishes_the_current_product_and_releases_the_lease(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p, worker, stop = await _started(rig, redis_client, redis_prefix)
    task = asyncio.create_task(worker.run(stop))
    await _wait_for(p.client, "set_price")
    stop.set()
    assert p.client.set_price_gate is not None
    p.client.set_price_gate.set()
    async with asyncio.timeout(SHUTDOWN_GRACE_SECONDS):
        await task
    assert p.client.methods().count("get_offers") == 1  # остальные товары не начаты
    assert p.client.methods().count("set_price") == 1
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["claimed_by"], row["claimed_until"]) == (None, None)
    rows = await fetch(rig, "SELECT status FROM price_write")
    assert rows == [("receipt",)]


async def test_stop_with_a_stuck_write_cancels_it_after_grace_and_keeps_the_hold(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "SHUTDOWN_GRACE_SECONDS", 0.2)
    p, worker, stop = await _started(rig, redis_client, redis_prefix)
    task = asyncio.create_task(worker.run(stop))
    await _wait_for(p.client, "set_price")
    stop.set()
    async with asyncio.timeout(5):
        await task  # затор в set_price не держит процесс дольше срока
    row = await shop_row(rig, p.shop.shop_id)
    assert (row["claimed_by"], row["claimed_until"]) == (None, None)
    ((status,),) = await fetch(rig, "SELECT status FROM price_write")
    assert status in {"sending", "unknown"}
    # повторный запуск продолжает: запрет не даёт отправить ту же цену второй раз
    p2 = await prepare(
        rig, redis_client, redis_prefix, shop=p.shop, database=rig.new_database(), worker_id="w2"
    )
    stop2 = asyncio.Event()
    again = asyncio.create_task(Worker(p2.ctx, idle_sleep=0.05).run(stop2))
    await _wait_for(p2.client, "find_product")
    stop2.set()
    async with asyncio.timeout(5):
        await again
    assert p.sku not in [snapshot.state.sku for snapshot, _ in p2.client.sent]


async def test_worker_exits_promptly_when_idle(
    rig: Rig, redis_client: Redis, redis_prefix: str
) -> None:
    p = await prepare(rig, redis_client, redis_prefix, dry_run=True)
    worker = Worker(p.ctx, idle_sleep=30)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    async with asyncio.timeout(2):
        await task


# --- Т65 ---


def test_entry_point_is_importable_without_side_effects() -> None:
    sys.modules.pop("kaspisa.worker.__main__", None)
    module = importlib.import_module("kaspisa.worker.__main__")
    assert callable(module.main)


def test_worker_config_defaults_to_dry_run_with_three_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KASPISA_DRY_RUN", raising=False)
    config = runner.worker_config(Settings(_env_file=None))
    assert (config.dry_run, config.slots) == (True, 3)
    assert config.worker_id


def test_dry_run_can_be_switched_off_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KASPISA_DRY_RUN", "false")
    assert runner.worker_config(Settings(_env_file=None)).dry_run is False


def test_worker_ids_are_unique_per_process_start() -> None:
    settings = Settings(_env_file=None)
    assert runner.worker_config(settings).worker_id != runner.worker_config(settings).worker_id


async def test_run_worker_stops_on_sigterm(
    rig: Rig, redis_client: Redis, redis_prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=rig.url,
        redis_url=f"redis://localhost:{redis_port()}/0",
    )
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
    async with asyncio.timeout(10):
        await runner.run_worker(settings)
    assert signal.getsignal(signal.SIGTERM) is not signal.SIG_IGN
