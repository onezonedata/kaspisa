"""Фикстуры тестов клиента Kaspi (не conftest.py: имя занято тестами базы, mypy видит дубликат).

Тестовые файлы импортируют нужные фикстуры явно."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import respx
from kaspi_testing import FakeTime, RecordingLimiter

from kaspisa.kaspi import KaspiClient


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def limiter(fake_time: FakeTime) -> RecordingLimiter:
    return RecordingLimiter(fake_time)


@pytest.fixture
def mock_kaspi() -> Iterator[respx.MockRouter]:
    """Заглушка сети: незаглушённый запрос — ошибка."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
async def client(
    mock_kaspi: respx.MockRouter, limiter: RecordingLimiter, fake_time: FakeTime
) -> AsyncIterator[KaspiClient]:
    async with KaspiClient(limiter=limiter, sleep=fake_time.sleep) as kaspi_client:
        yield kaspi_client
