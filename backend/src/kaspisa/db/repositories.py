"""Репозитории арендатора. Каждый запрос содержит фильтр по tenant_id."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    and_,
    case,
    delete,
    func,
    literal,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.base import instance_state

from kaspisa.db.base import TenantId
from kaspisa.db.errors import NotFoundError, StaleStateError, run_guarded
from kaspisa.db.models import (
    DEFAULT_CITY_ID,
    OwnerEvent,
    OwnerEventKind,
    PriceChange,
    PriceChangeResult,
    PriceWrite,
    PriceWriteStatus,
    Product,
    Shop,
    ShopConnectionState,
    SkipReason,
    TelegramSettings,
    Tenant,
    User,
)
from kaspisa.pricing.types import PricingSettings, Strategy
from kaspisa.security import Secret, SecretCipher, SecretContext, SecretField


@dataclass(frozen=True)
class ShopCredentials:
    """Логин и пароль кабинета Kaspi; repr безопасен за счёт Secret."""

    login: Secret
    password: Secret


def normalize_email(email: str) -> str:
    """Единственное место правила записи e-mail в базе: нижний регистр."""
    return email.lower()


class UserRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(self, *, email: str, password_hash: str) -> User:
        user = User(
            tenant_id=self._tenant_id, email=normalize_email(email), password_hash=password_hash
        )
        self._session.add(user)
        await run_guarded(self._session, self._session.flush)
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.tenant_id == self._tenant_id, User.id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list(self) -> Sequence[User]:
        stmt = (
            select(User).where(User.tenant_id == self._tenant_id).order_by(User.created_at, User.id)
        )
        return (await self._session.execute(stmt)).scalars().all()


# Причина остановки, которую ставит серия ошибок входа (запрещает и загрузку каталога).
LOGIN_HALT_REASON = "login_errors"

# Блок уровня арендатора, не магазина: так в контексте шифрования стоит «магазин» токена Telegram.
NO_SHOP = uuid.UUID(int=0)


@dataclass(frozen=True)
class TelegramTarget:
    """Расшифрованные настройки отправки; repr безопасен за счёт Secret."""

    token: Secret
    chat_id: str


class TelegramRepository:
    """Настройки Telegram арендатора (ADR 0025): токен зашифрован, строка одна."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        cipher_provider: Callable[[], SecretCipher],
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._cipher_provider = cipher_provider

    def _context(self) -> SecretContext:
        return SecretContext(self._tenant_id, NO_SHOP, SecretField.TELEGRAM_TOKEN)

    async def get(self) -> TelegramSettings | None:
        """Строка арендатора без расшифровки."""
        stmt = select(TelegramSettings).where(TelegramSettings.tenant_id == self._tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_target(self) -> TelegramTarget | None:
        """Токен и chat id; нечитаемый блок даёт DecryptionError."""
        row = await self.get()
        if row is None:
            return None
        token = self._cipher_provider().decrypt(row.token_enc, self._context())
        return TelegramTarget(token=token, chat_id=row.chat_id)

    async def save(self, *, chat_id: str, token: Secret | None) -> TelegramSettings:
        """Создаёт или меняет строку. `token=None` оставляет токен; строки нет — NotFoundError."""
        stmt: Any
        if token is None:
            stmt = (
                update(TelegramSettings)
                .where(TelegramSettings.tenant_id == self._tenant_id)
                .values(chat_id=chat_id)
                .returning(TelegramSettings)
                .execution_options(populate_existing=True)
            )
        else:
            blob = self._cipher_provider().encrypt(token, self._context())
            insert_stmt = pg_insert(TelegramSettings).values(
                tenant_id=self._tenant_id, token_enc=blob, chat_id=chat_id
            )
            stmt = (
                insert_stmt.on_conflict_do_update(
                    index_elements=[TelegramSettings.tenant_id],
                    set_={"token_enc": insert_stmt.excluded.token_enc, "chat_id": chat_id},
                )
                .returning(TelegramSettings)
                .execution_options(populate_existing=True)
            )
        row = await run_guarded(self._session, lambda: self._execute_save(stmt))
        if row is None:
            raise NotFoundError
        return row

    async def _execute_save(self, stmt: Any) -> TelegramSettings | None:
        await self._session.flush()
        row: TelegramSettings | None = (await self._session.execute(stmt)).scalar_one_or_none()
        return row

    async def delete(self) -> bool:
        stmt = (
            delete(TelegramSettings)
            .where(TelegramSettings.tenant_id == self._tenant_id)
            .returning(TelegramSettings.tenant_id)
        )
        deleted = (await self._session.execute(stmt)).first() is not None
        for obj in list(self._session.identity_map.values()):
            if isinstance(obj, TelegramSettings) and obj.tenant_id == self._tenant_id:
                self._session.expunge(obj)
        return deleted


class ShopRepository:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        cipher_provider: Callable[[], SecretCipher],
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._cipher_provider = cipher_provider

    def _encrypt(self, value: Secret, shop_id: uuid.UUID, field: SecretField) -> bytes:
        context = SecretContext(self._tenant_id, shop_id, field)
        return self._cipher_provider().encrypt(value, context)

    def _decrypt(self, blob: bytes, shop_id: uuid.UUID, field: SecretField) -> Secret:
        context = SecretContext(self._tenant_id, shop_id, field)
        return self._cipher_provider().decrypt(blob, context)

    async def lock_owner(self) -> None:
        """Блокирует строку арендатора до конца транзакции (ADR 0029).

        `FOR NO KEY UPDATE` ждёт другую такую же блокировку, но не мешает вставкам
        с внешним ключом на `tenant` (они берут `FOR KEY SHARE`).
        """
        stmt = select(Tenant.id).where(Tenant.id == self._tenant_id).with_for_update(key_share=True)
        if (await self._session.execute(stmt)).first() is None:
            raise NotFoundError

    async def add(self, *, login: Secret, password: Secret) -> Shop:
        shop_id = uuid.uuid4()  # нужен до INSERT: входит в контекст шифрования
        shop = Shop(
            id=shop_id,
            tenant_id=self._tenant_id,
            login_enc=self._encrypt(login, shop_id, SecretField.SHOP_LOGIN),
            password_enc=self._encrypt(password, shop_id, SecretField.SHOP_PASSWORD),
            city_id=DEFAULT_CITY_ID,
        )
        self._session.add(shop)
        await run_guarded(self._session, self._session.flush)
        return shop

    async def get(self, shop_id: uuid.UUID) -> Shop | None:
        stmt = select(Shop).where(Shop.tenant_id == self._tenant_id, Shop.id == shop_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _require(self, shop_id: uuid.UUID) -> Shop:
        shop = await self.get(shop_id)
        if shop is None:
            raise NotFoundError
        return shop

    async def list(self) -> Sequence[Shop]:
        stmt = (
            select(Shop).where(Shop.tenant_id == self._tenant_id).order_by(Shop.created_at, Shop.id)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def delete(self, shop_id: uuid.UUID) -> None:
        stmt = (
            delete(Shop)
            .where(Shop.tenant_id == self._tenant_id, Shop.id == shop_id)
            .returning(Shop.id)
        )
        if (await self._session.execute(stmt)).first() is None:
            raise NotFoundError
        # Строки удалены каскадом на стороне базы: отсоединяем только объекты этого магазина.
        # Владельца определяем без подгрузки: устаревшие после отката объекты не читаем.
        for obj in list(self._session.identity_map.values()):
            if not isinstance(obj, Shop | Product | PriceChange | PriceWrite):
                continue
            state = instance_state(obj)
            if isinstance(obj, Shop):
                identity = state.identity
                owner = identity[0] if identity else None
            else:
                owner = state.dict.get("shop_id")
            if owner == shop_id:
                self._session.expunge(obj)

    async def get_credentials(self, shop_id: uuid.UUID) -> ShopCredentials:
        shop = await self._require(shop_id)
        return ShopCredentials(
            login=self._decrypt(shop.login_enc, shop_id, SecretField.SHOP_LOGIN),
            password=self._decrypt(shop.password_enc, shop_id, SecretField.SHOP_PASSWORD),
        )

    async def get_session(self, shop_id: uuid.UUID) -> Secret | None:
        shop = await self._require(shop_id)
        if shop.session_enc is None:
            return None
        return self._decrypt(shop.session_enc, shop_id, SecretField.SHOP_SESSION)

    async def _update(
        self,
        shop_id: uuid.UUID,
        values: dict[str, Any],
        *,
        expected_version: int | None,
        bump_version: bool,
    ) -> Shop:
        """UPDATE одной строкой; условный по версии, если она задана."""
        if bump_version:
            values = {**values, "connection_version": Shop.connection_version + 1}
        stmt = update(Shop).where(Shop.tenant_id == self._tenant_id, Shop.id == shop_id)
        if expected_version is not None:
            stmt = stmt.where(Shop.connection_version == expected_version)
        stmt = stmt.values(**values).returning(Shop).execution_options(populate_existing=True)
        shop = await run_guarded(self._session, lambda: self._execute_update(stmt))
        if shop is not None:
            return shop
        # Строк нет: либо магазина нет у арендатора, либо версия устарела.
        await self._require(shop_id)
        raise StaleStateError

    async def _execute_update(self, stmt: Any) -> Shop | None:
        # autoflush выключен: без flush UPDATE ... RETURNING с populate_existing
        # затёр бы несохранённые присваивания этого же магазина.
        await self._session.flush()
        result = await self._session.execute(stmt)
        shop: Shop | None = result.scalar_one_or_none()
        if shop is not None:
            await self._session.flush()
        return shop

    async def set_credentials(self, shop_id: uuid.UUID, *, login: Secret, password: Secret) -> Shop:
        return await self._update(
            shop_id,
            {
                "login_enc": self._encrypt(login, shop_id, SecretField.SHOP_LOGIN),
                "password_enc": self._encrypt(password, shop_id, SecretField.SHOP_PASSWORD),
                "connection_state": ShopConnectionState.NOT_CONNECTED.value,
                "connection_error": None,
                "connection_changed_at": func.clock_timestamp(),
            },
            expected_version=None,
            bump_version=True,
        )

    async def save_session(
        self, shop_id: uuid.UUID, session_state: Secret, *, expected_version: int
    ) -> Shop:
        return await self._update(
            shop_id,
            {"session_enc": self._encrypt(session_state, shop_id, SecretField.SHOP_SESSION)},
            expected_version=expected_version,
            bump_version=False,
        )

    async def mark_awaiting_code(
        self, shop_id: uuid.UUID, session_state: Secret, *, expected_version: int
    ) -> Shop:
        return await self._update(
            shop_id,
            {
                "connection_state": ShopConnectionState.AWAITING_CODE.value,
                "connection_error": None,
                "connection_changed_at": func.clock_timestamp(),
                "session_enc": self._encrypt(session_state, shop_id, SecretField.SHOP_SESSION),
            },
            expected_version=expected_version,
            bump_version=True,
        )

    async def mark_connected(
        self,
        shop_id: uuid.UUID,
        *,
        merchant_uid: str,
        session_state: Secret,
        expected_version: int,
        auto_login: bool = False,
    ) -> Shop:
        """Подключён. `auto_login=True` помечает версию как созданную автовходом."""
        return await self._update(
            shop_id,
            {
                "connection_state": ShopConnectionState.CONNECTED.value,
                "connection_error": None,
                "connection_changed_at": func.clock_timestamp(),
                "merchant_uid": merchant_uid,
                "session_enc": self._encrypt(session_state, shop_id, SecretField.SHOP_SESSION),
                # В SET справа старое значение: «версия + 1» равна новой версии.
                "unproven_login_version": (Shop.connection_version + 1 if auto_login else None),
            },
            expected_version=expected_version,
            bump_version=True,
        )

    async def mark_reauth_required(
        self, shop_id: uuid.UUID, *, error: str, expected_version: int
    ) -> Shop:
        return await self._update(
            shop_id,
            {
                "connection_state": ShopConnectionState.REAUTH_REQUIRED.value,
                "connection_error": error,
                "connection_changed_at": func.clock_timestamp(),
            },
            expected_version=expected_version,
            bump_version=True,
        )

    # --- обработчик: аренда, серии ошибок, остановка (ADR 0008) ---

    async def _lease_update(
        self, shop_id: uuid.UUID, worker_id: str, values: dict[str, Any]
    ) -> bool:
        """UPDATE аренды только владельцем; False, если аренды у этого `worker_id` нет."""
        stmt = (
            update(Shop)
            .where(
                Shop.tenant_id == self._tenant_id,
                Shop.id == shop_id,
                Shop.claimed_by == worker_id,
            )
            .values(**values)
            .returning(Shop.id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None

    async def release_lease(self, shop_id: uuid.UUID, worker_id: str) -> bool:
        return await self._lease_update(
            shop_id, worker_id, {"claimed_by": None, "claimed_until": None}
        )

    async def renew_lease(self, shop_id: uuid.UUID, worker_id: str, until: datetime) -> bool:
        return await self._lease_update(shop_id, worker_id, {"claimed_until": until})

    async def defer(self, shop_id: uuid.UUID, worker_id: str, until: datetime) -> bool:
        """Срок пропуска магазина: владелец остаётся, магазин не отдаётся до `until`."""
        return await self._lease_update(shop_id, worker_id, {"claimed_until": until})

    async def _record_outcome(
        self,
        shop_id: uuid.UUID,
        *,
        column: Any,
        reason: str,
        ok: bool,
        limit: int,
        now: datetime,
    ) -> Shop:
        """Серия подряд идущих ошибок одним UPDATE; на пороге ставит остановку."""
        if ok:
            values: dict[str, Any] = {column.key: 0}
        else:
            reached = (Shop.halt_reason.is_(None)) & (column + 1 >= limit)
            values = {
                column.key: column + 1,
                "halt_reason": case((reached, reason), else_=Shop.halt_reason),
                "halted_at": case(
                    (reached, literal(now, DateTime(timezone=True))), else_=Shop.halted_at
                ),
            }
        return await self._update(shop_id, values, expected_version=None, bump_version=False)

    async def record_login_outcome(
        self, shop_id: uuid.UUID, ok: bool, limit: int, now: datetime
    ) -> Shop:
        return await self._record_outcome(
            shop_id,
            column=Shop.login_error_streak,
            reason=LOGIN_HALT_REASON,
            ok=ok,
            limit=limit,
            now=now,
        )

    async def record_write_outcome(
        self, shop_id: uuid.UUID, *, ok: bool, limit: int, now: datetime
    ) -> Shop:
        return await self._record_outcome(
            shop_id,
            column=Shop.write_error_streak,
            reason="write_errors",
            ok=ok,
            limit=limit,
            now=now,
        )

    async def mark_catalog_synced(self, shop_id: uuid.UUID, now: datetime) -> Shop:
        """Каталог загружен: ставит метку и снимает паузу после сбоя."""
        return await self._update(
            shop_id,
            {"catalog_synced_at": now, "catalog_retry_at": None},
            expected_version=None,
            bump_version=False,
        )

    async def defer_catalog(self, shop_id: uuid.UUID, until: datetime) -> Shop:
        """Пауза автоматической загрузки каталога до `until` (метка загрузки не меняется)."""
        return await self._update(
            shop_id, {"catalog_retry_at": until}, expected_version=None, bump_version=False
        )

    async def halt(self, shop_id: uuid.UUID, reason: str, now: datetime) -> Shop:
        """Останавливает бота магазина; уже стоящая причина не затирается."""
        free = Shop.halt_reason.is_(None)
        return await self._update(
            shop_id,
            {
                "halt_reason": case((free, reason), else_=Shop.halt_reason),
                "halted_at": case(
                    (free, literal(now, DateTime(timezone=True))), else_=Shop.halted_at
                ),
            },
            expected_version=None,
            bump_version=False,
        )

    async def clear_halt(self, shop_id: uuid.UUID) -> Shop:
        """Снимает остановку и обнуляет серии. Вызывает человек, не обработчик."""
        return await self._update(
            shop_id,
            {
                "halt_reason": None,
                "halted_at": None,
                "login_error_streak": 0,
                "write_error_streak": 0,
            },
            expected_version=None,
            bump_version=False,
        )

    async def lock_for_write(self, shop_id: uuid.UUID) -> Shop:
        """Блокирует строку магазина до commit и отдаёт свежие значения."""
        stmt = (
            select(Shop)
            .where(Shop.tenant_id == self._tenant_id, Shop.id == shop_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        shop = (await self._session.execute(stmt)).scalar_one_or_none()
        if shop is None:
            raise NotFoundError
        return shop

    async def clear_unproven_login(self, shop_id: uuid.UUID, expected_version: int) -> bool:
        """Очищает признак автовхода, только если он относится к `expected_version`."""
        stmt = (
            update(Shop)
            .where(
                Shop.tenant_id == self._tenant_id,
                Shop.id == shop_id,
                Shop.unproven_login_version == expected_version,
            )
            .values(unproven_login_version=None)
            .returning(Shop.id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None


class ProductRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(self, *, shop_id: uuid.UUID, sku: str, master_sku: str, title: str) -> Product:
        step_stmt = select(Shop.default_step).where(
            Shop.tenant_id == self._tenant_id, Shop.id == shop_id
        )
        step = (await self._session.execute(step_stmt)).scalar_one_or_none()
        if step is None:
            raise NotFoundError
        product = Product(
            tenant_id=self._tenant_id,
            shop_id=shop_id,
            sku=sku,
            master_sku=master_sku,
            title=title,
            step=step,
        )
        self._session.add(product)
        await run_guarded(self._session, self._session.flush)
        return product

    async def count_for_shop(self, shop_id: uuid.UUID) -> tuple[int, int]:
        """Число товаров магазина и из них отслеживаемых: один запрос."""
        stmt = select(
            func.count(),
            func.count().filter(Product.tracked.is_(True)),
        ).where(Product.tenant_id == self._tenant_id, Product.shop_id == shop_id)
        total, tracked = (await self._session.execute(stmt)).one()
        return int(total), int(tracked)

    async def get(self, product_id: int) -> Product | None:
        stmt = select(Product).where(Product.tenant_id == self._tenant_id, Product.id == product_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, product_id: int) -> Product | None:
        """Блокирует строку до commit и отдаёт свежие значения из базы."""
        stmt = (
            select(Product)
            .where(Product.tenant_id == self._tenant_id, Product.id == product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_sku(self, shop_id: uuid.UUID, sku: str) -> Product | None:
        stmt = select(Product).where(
            Product.tenant_id == self._tenant_id, Product.shop_id == shop_id, Product.sku == sku
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_shop(
        self, shop_id: uuid.UUID, *, limit: int, offset: int = 0
    ) -> Sequence[Product]:
        stmt = (
            select(Product)
            .where(Product.tenant_id == self._tenant_id, Product.shop_id == shop_id)
            .order_by(Product.id)
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.execute(stmt)).scalars().all()

    # --- обработчик: планировщик, наблюдения, отказы ---

    async def next_due(
        self,
        shop_id: uuid.UUID,
        *,
        now: datetime,
        recheck_seconds: int,
        unbounded_recheck_seconds: int,
        bounded: bool,
    ) -> Product | None:
        """Товар, который пора проверять: самый давно проверенный (NULL первым).

        `bounded=True` - товары с обеими границами (интервал `recheck_seconds`),
        `False` - без границы хотя бы одной (интервал `unbounded_recheck_seconds`).
        """
        if bounded:
            cutoff = now - timedelta(seconds=recheck_seconds)
            bounds = and_(Product.min_price.is_not(None), Product.max_price.is_not(None))
        else:
            cutoff = now - timedelta(seconds=unbounded_recheck_seconds)
            bounds = or_(Product.min_price.is_(None), Product.max_price.is_(None))
        stmt = (
            select(Product)
            .where(
                Product.tenant_id == self._tenant_id,
                Product.shop_id == shop_id,
                Product.tracked.is_(True),
                bounds,
                (Product.last_checked_at.is_(None)) | (Product.last_checked_at <= cutoff),
                (Product.retry_at.is_(None)) | (Product.retry_at <= now),
            )
            .order_by(Product.last_checked_at.asc().nulls_first(), Product.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _update_values(self, product_id: int, values: dict[str, Any]) -> None:
        stmt = (
            update(Product)
            .where(Product.tenant_id == self._tenant_id, Product.id == product_id)
            .values(**values)
            .returning(Product.id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        if result.first() is None:
            raise NotFoundError

    async def record_observation(
        self,
        product_id: int,
        *,
        current_price: int | None,
        position: int | None,
        position_over: int | None,
        competitors_count: int | None,
        first_place_price: int | None,
        decision_reason: str | None,
        decision_target_price: int | None,
        checked_at: datetime,
    ) -> None:
        """Наблюдение и решение «не менять»; для решения «менять» передают `None, None`.

        Итог записи (`decision_write`) обнуляется: его пишет `record_write` после записи цены.
        """
        await self._update_values(
            product_id,
            {
                "current_price": current_price,
                "position": position,
                "position_over": position_over,
                "competitors_count": competitors_count,
                "first_place_price": first_place_price,
                "last_checked_at": checked_at,
                "skip_reason": None,  # успешная проверка снимает причину пропуска
                "skipped_at": None,
                "decision_reason": decision_reason,
                "decision_target_price": decision_target_price,
                "decision_write": None,
            },
        )

    async def record_write(
        self,
        product_id: int,
        *,
        decision_reason: str,
        decision_target_price: int,
        decision_write: str,
        checked_at: datetime,
    ) -> None:
        """Решение «менять» с итогом записи цены (ADR 0020)."""
        await self._update_values(
            product_id,
            {
                "decision_reason": decision_reason,
                "decision_target_price": decision_target_price,
                "decision_write": decision_write,
                "last_checked_at": checked_at,
            },
        )

    async def record_skip(self, product_id: int, reason: SkipReason, checked_at: datetime) -> None:
        """Проверка пропущена: отметка времени и код причины (ADR 0017); решение обнуляется."""
        await self._update_values(
            product_id,
            {
                "last_checked_at": checked_at,
                "skip_reason": reason.value,
                "skipped_at": checked_at,
                "decision_reason": None,
                "decision_target_price": None,
                "decision_write": None,
            },
        )

    async def touch_checked(self, product_id: int, checked_at: datetime) -> None:
        """Только отметка проверки; причину пропуска не трогает."""
        await self._update_values(product_id, {"last_checked_at": checked_at})

    async def set_refusal(
        self,
        product_id: int,
        *,
        reason: str,
        since: datetime | None,
        fingerprint: str | None,
        retry_at: datetime | None,
    ) -> None:
        await self._update_values(
            product_id,
            {
                "refusal_reason": reason,
                "refusal_since": since,
                "refusal_fingerprint": fingerprint,
                "retry_at": retry_at,
            },
        )

    async def clear_refusal(self, product_id: int) -> None:
        await self._update_values(
            product_id,
            {
                "refusal_reason": None,
                "refusal_since": None,
                "refusal_fingerprint": None,
                "retry_at": None,
            },
        )


class PriceChangeRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(
        self,
        *,
        product_id: int,
        old_price: int,
        new_price: int,
        reason: str,
        result: PriceChangeResult,
        target_price: int | None = None,
        competitor_merchant_id: str | None = None,
        competitor_name: str | None = None,
        competitor_price: int | None = None,
        capped_at_max: bool = False,
        skipped_competitors: int = 0,
        cities_aligned: bool = False,
        error: str | None = None,
        created_at: datetime | None = None,
    ) -> PriceChange:
        shop_stmt = select(Product.shop_id).where(
            Product.tenant_id == self._tenant_id, Product.id == product_id
        )
        shop_id = (await self._session.execute(shop_stmt)).scalar_one_or_none()
        if shop_id is None:
            raise NotFoundError
        change = PriceChange(
            tenant_id=self._tenant_id,
            shop_id=shop_id,
            product_id=product_id,
            old_price=old_price,
            new_price=new_price,
            reason=reason,
            result=result.value,
            target_price=target_price,
            competitor_merchant_id=competitor_merchant_id,
            competitor_name=competitor_name,
            competitor_price=competitor_price,
            capped_at_max=capped_at_max,
            skipped_competitors=skipped_competitors,
            cities_aligned=cities_aligned,
            error=error,
        )
        if created_at is not None:  # иначе время ставит база
            change.created_at = created_at
        self._session.add(change)
        await run_guarded(self._session, self._session.flush)
        return change

    async def list_for_shop(
        self, shop_id: uuid.UUID, *, limit: int, offset: int = 0
    ) -> Sequence[PriceChange]:
        stmt = (
            select(PriceChange)
            .where(PriceChange.tenant_id == self._tenant_id, PriceChange.shop_id == shop_id)
            .order_by(PriceChange.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def list_for_product(
        self, product_id: int, *, limit: int, offset: int = 0
    ) -> Sequence[PriceChange]:
        stmt = (
            select(PriceChange)
            .where(PriceChange.tenant_id == self._tenant_id, PriceChange.product_id == product_id)
            .order_by(PriceChange.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.execute(stmt)).scalars().all()

    # --- обработчик: результат, квота, компенсация ---

    async def set_result(
        self, change_id: int, result: PriceChangeResult, error: str | None
    ) -> None:
        stmt = (
            update(PriceChange)
            .where(PriceChange.tenant_id == self._tenant_id, PriceChange.id == change_id)
            .values(result=result.value, error=error)
            .returning(PriceChange.id)
        )
        found = await run_guarded(self._session, lambda: self._session.execute(stmt))
        if found.first() is None:
            raise NotFoundError

    def _sent_since(self, shop_id: uuid.UUID, since: datetime) -> Any:
        return select(PriceChange.created_at).where(
            PriceChange.tenant_id == self._tenant_id,
            PriceChange.shop_id == shop_id,
            PriceChange.result.in_(
                (PriceChangeResult.APPLIED.value, PriceChangeResult.UNCONFIRMED.value)
            ),
            PriceChange.created_at > since,
        )

    async def count_sent_since(self, shop_id: uuid.UUID, since: datetime) -> int:
        """Отправки (applied и unconfirmed) после `since`: счётчик квоты магазина."""
        sent = self._sent_since(shop_id, since).subquery()
        stmt = select(func.count()).select_from(sent)
        return int((await self._session.execute(stmt)).scalar_one())

    async def has_sent_since(self, product_id: int, since: datetime) -> bool:
        """Была ли у товара отправка (applied или unconfirmed) строго после `since`."""
        stmt = (
            select(PriceChange.id)
            .where(
                PriceChange.tenant_id == self._tenant_id,
                PriceChange.product_id == product_id,
                PriceChange.result.in_(
                    (PriceChangeResult.APPLIED.value, PriceChangeResult.UNCONFIRMED.value)
                ),
                PriceChange.created_at > since,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def quota_free_at(
        self, shop_id: uuid.UUID, *, now: datetime, limit: int, window_seconds: int
    ) -> datetime | None:
        """Когда квота освободится; None, если отправок в окне меньше `limit`."""
        window = timedelta(seconds=window_seconds)
        stmt = self._sent_since(shop_id, now - window).order_by(PriceChange.created_at)
        times = (await self._session.execute(stmt)).scalars().all()
        if len(times) < limit:
            return None
        free_from: datetime = times[len(times) - limit]
        return free_from + window

    async def discard_unsent(self, change_id: int) -> bool:
        """Удаляет строку `unconfirmed`: запись заведомо не ушла. Прочие строки не трогает."""
        stmt = (
            delete(PriceChange)
            .where(
                PriceChange.tenant_id == self._tenant_id,
                PriceChange.id == change_id,
                PriceChange.result == PriceChangeResult.UNCONFIRMED.value,
            )
            .returning(PriceChange.id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None


class _Unset:
    """Метка «значение не передано» (отличает от None)."""


UNSET = _Unset()

# Зеркало PRICE_WRITE_HOLD_SECONDS и PRICE_VERIFY_FIRST_CHECK_SECONDS из kaspisa.kaspi:
# слой данных не зависит от клиента; равенство проверяет тест.
_HOLD_SECONDS = 600
_FIRST_CHECK_SECONDS = 30
_HOLDING = (
    PriceWriteStatus.SENDING.value,
    PriceWriteStatus.RECEIPT.value,
    PriceWriteStatus.UNKNOWN.value,
)


class PriceWriteRepository:
    """Запрет повторной записи и данные подтверждения (одна строка на товар)."""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def get(self, product_id: int) -> PriceWrite | None:
        stmt = (
            select(PriceWrite)
            .where(PriceWrite.tenant_id == self._tenant_id, PriceWrite.product_id == product_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def open(
        self,
        product_id: int,
        *,
        snapshot: Mapping[str, Any],
        expected: Mapping[str, int],
        sent_at: datetime,
        price_change_id: int,
        now: datetime,
        hold_seconds: int = _HOLD_SECONDS,
        first_check_seconds: int = _FIRST_CHECK_SECONDS,
    ) -> bool:
        """Создаёт или заменяет строку; False, если запрет ещё действует.

        Один оператор INSERT ... ON CONFLICT: два одновременных вызова дают одну строку.
        """
        owner = select(Product.shop_id, Product.sku).where(
            Product.tenant_id == self._tenant_id, Product.id == product_id
        )
        found = (await self._session.execute(owner)).one_or_none()
        if found is None:
            raise NotFoundError
        values: dict[str, Any] = {
            "product_id": product_id,
            "tenant_id": self._tenant_id,
            "shop_id": found.shop_id,
            "sku": found.sku,
            "snapshot": dict(snapshot),
            "expected_prices": dict(expected),
            "sent_at": sent_at,
            "status": PriceWriteStatus.SENDING.value,
            "job_id": None,
            "next_verify_at": sent_at + timedelta(seconds=first_check_seconds),
            "deadline_reported": False,
            "error_counted": False,
            "price_change_id": price_change_id,
        }
        insert = pg_insert(PriceWrite).values(**values)
        holding = PriceWrite.status.in_(_HOLDING) & (
            literal(now, DateTime(timezone=True))
            < PriceWrite.sent_at + timedelta(seconds=hold_seconds)
        )
        stmt = insert.on_conflict_do_update(
            index_elements=[PriceWrite.product_id],
            set_={key: insert.excluded[key] for key in values if key != "product_id"},
            where=not_(holding),
        ).returning(PriceWrite.product_id)
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None

    async def set_status(
        self,
        product_id: int,
        status: str,
        *,
        job_id: str | None = None,
        next_verify_at: datetime | _Unset | None = UNSET,
        deadline_reported: bool | _Unset = UNSET,
        error_counted: bool | _Unset = UNSET,
    ) -> None:
        """Меняет статус; `job_id=None` и непереданные поля остаются как были."""
        values: dict[str, Any] = {"status": status}
        if job_id is not None:
            values["job_id"] = job_id
        if not isinstance(next_verify_at, _Unset):
            values["next_verify_at"] = next_verify_at
        if not isinstance(deadline_reported, _Unset):
            values["deadline_reported"] = deadline_reported
        if not isinstance(error_counted, _Unset):
            values["error_counted"] = error_counted
        stmt = (
            update(PriceWrite)
            .where(PriceWrite.tenant_id == self._tenant_id, PriceWrite.product_id == product_id)
            .values(**values)
            .returning(PriceWrite.product_id)
            .execution_options(synchronize_session=False)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        if result.first() is None:
            raise NotFoundError

    async def delete(self, product_id: int) -> bool:
        stmt = (
            delete(PriceWrite)
            .where(PriceWrite.tenant_id == self._tenant_id, PriceWrite.product_id == product_id)
            .returning(PriceWrite.product_id)
            .execution_options(synchronize_session=False)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None

    async def due_for_verification(self, shop_id: uuid.UUID, now: datetime) -> Sequence[PriceWrite]:
        stmt = (
            select(PriceWrite)
            .where(
                PriceWrite.tenant_id == self._tenant_id,
                PriceWrite.shop_id == shop_id,
                PriceWrite.next_verify_at.is_not(None),
                PriceWrite.next_verify_at <= now,
            )
            .order_by(PriceWrite.next_verify_at, PriceWrite.product_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def next_verify_time(self, shop_id: uuid.UUID) -> datetime | None:
        """Ближайший срок проверки открытой записи магазина; `None`, если открытых нет."""
        stmt = select(func.min(PriceWrite.next_verify_at)).where(
            PriceWrite.tenant_id == self._tenant_id,
            PriceWrite.shop_id == shop_id,
            PriceWrite.next_verify_at.is_not(None),
        )
        return (await self._session.execute(stmt)).scalar_one()


_SHOP_EVENTS = frozenset({OwnerEventKind.BOT_MODE_CHANGED, OwnerEventKind.BOUNDS_CHANGED})


class OwnerEventRepository:
    """Журнал действий владельца (ADR 0024): запись и чтение последних событий арендатора."""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(
        self,
        *,
        kind: OwnerEventKind,
        user_id: uuid.UUID,
        ip: str,
        shop_id: uuid.UUID | None = None,
        sku: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
    ) -> OwnerEvent:
        """`tenant_id` ставит репозиторий. `shop_id` обязателен для смены режима и границ."""
        if (kind in _SHOP_EVENTS) != (shop_id is not None):
            raise ValueError(f"shop_id и вид события {kind.value} не согласованы")
        event = OwnerEvent(
            tenant_id=self._tenant_id,
            user_id=user_id,
            kind=kind.value,
            ip=ip,
            shop_id=shop_id,
            sku=sku,
            old_value=old_value,
            new_value=new_value,
        )
        self._session.add(event)
        await run_guarded(self._session, self._session.flush)
        return event

    async def list_recent(self, limit: int) -> Sequence[OwnerEvent]:
        stmt = (
            select(OwnerEvent)
            .where(OwnerEvent.tenant_id == self._tenant_id)
            .order_by(OwnerEvent.created_at.desc(), OwnerEvent.id.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()


def pricing_settings_for(shop: Shop, product: Product) -> PricingSettings | None:
    """Настройки движка цен; None, если не заданы границы цены или нет merchant_uid.

    Режим бота и city_id не проверяются: это решает обработчик.
    """
    if product.min_price is None or product.max_price is None or shop.merchant_uid is None:
        return None
    return PricingSettings(
        strategy=Strategy(shop.strategy),
        auto_decrease=product.auto_decrease,
        auto_raise=product.auto_raise,
        shop_auto_raise=shop.auto_raise_enabled,
        min_price=product.min_price,
        max_price=product.max_price,
        step=product.step,
        own_merchant_id=shop.merchant_uid,
        excluded_merchant_ids=frozenset(shop.excluded_merchant_ids),
    )
