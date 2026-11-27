"""Точка входа слоя данных: сессия наружу не выдаётся, только области."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kaspisa.db.base import TenantId
from kaspisa.db.errors import run_guarded
from kaspisa.db.repositories import (
    OwnerEventRepository,
    PriceChangeRepository,
    PriceWriteRepository,
    ProductRepository,
    ShopRepository,
    TelegramRepository,
    UserRepository,
)
from kaspisa.db.system import SystemScope
from kaspisa.security import SecretCipher, load_keyring

CipherProvider = Callable[[], SecretCipher]


class TenantScope:
    """Одна сессия и одна транзакция с репозиториями одного арендатора."""

    def __init__(
        self, session: AsyncSession, tenant_id: TenantId, cipher_provider: CipherProvider
    ) -> None:
        self._session = session
        self.tenant_id = tenant_id
        self.users = UserRepository(session, tenant_id)
        self.shops = ShopRepository(session, tenant_id, cipher_provider)
        self.products = ProductRepository(session, tenant_id)
        self.price_changes = PriceChangeRepository(session, tenant_id)
        self.price_writes = PriceWriteRepository(session, tenant_id)
        self.owner_events = OwnerEventRepository(session, tenant_id)
        self.telegram = TelegramRepository(session, tenant_id, cipher_provider)

    async def commit(self) -> None:
        await run_guarded(self._session, self._session.commit)


class Database:
    """Единственная точка входа: области арендатора и системные запросы."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: SecretCipher | None = None,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._engine = engine

    def _get_cipher(self) -> SecretCipher:
        # Ключ читается при первом обращении к магазинам, а не при создании.
        if self._cipher is None:
            self._cipher = SecretCipher(load_keyring())
        return self._cipher

    def tenant(self, tenant_id: TenantId) -> AbstractAsyncContextManager[TenantScope]:
        return self._tenant_scope(tenant_id)

    def system(self) -> AbstractAsyncContextManager[SystemScope]:
        return self._system_scope()

    @asynccontextmanager
    async def _tenant_scope(self, tenant_id: TenantId) -> AsyncIterator[TenantScope]:
        async with self._session_factory() as session:
            yield TenantScope(session, tenant_id, self._get_cipher)

    @asynccontextmanager
    async def _system_scope(self) -> AsyncIterator[SystemScope]:
        async with self._session_factory() as session:
            yield SystemScope(session, self._get_cipher)

    async def dispose(self) -> None:
        """Закрыть соединения при остановке процесса."""
        if self._engine is not None:
            await self._engine.dispose()


def create_database(url: str, *, cipher: SecretCipher | None = None) -> Database:
    # autoflush=False: отправка в базу только через flush/commit под run_guarded,
    # иначе IntegrityError с DETAIL строки выходит из обычного запроса.
    # hide_parameters: значения запросов (хеши, шифртексты) не попадают в тексты ошибок.
    engine = create_async_engine(url, hide_parameters=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return Database(factory, cipher=cipher, engine=engine)
