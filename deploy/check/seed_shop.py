"""Приёмка копий (Т38, Т39): заводит магазин с выдуманными доступами, зашифрованными ключом установки.

Запуск внутри контейнера api: `docker compose exec -T api python - <ID арендатора> < seed_shop.py`.
Печатает только идентификатор магазина.
"""

import asyncio
import sys
import uuid

from kaspisa.config import Settings
from kaspisa.db.base import TenantId
from kaspisa.db.database import create_database
from kaspisa.security import Secret


async def main(tenant_id: str) -> None:
    database = create_database(Settings().database_url)
    try:
        async with database.tenant(TenantId(uuid.UUID(tenant_id))) as scope:
            shop = await scope.shops.add(
                login=Secret("seed-login@example.test"), password=Secret("seed-password-not-real")
            )
            await scope.commit()
            print(shop.id)
    finally:
        await database.dispose()


asyncio.run(main(sys.argv[1]))
