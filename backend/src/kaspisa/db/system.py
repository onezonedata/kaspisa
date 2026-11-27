"""Запросы без арендатора. Единственное место, где запрос не фильтруется по арендатору."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import ARRAY, DateTime, Integer, bindparam, exists, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from kaspisa.db.base import TenantId
from kaspisa.db.errors import run_guarded
from kaspisa.db.models import Product, Shop, TelegramSettings, Tenant, User
from kaspisa.db.repositories import LOGIN_HALT_REASON, NO_SHOP, normalize_email
from kaspisa.security import (
    DecryptionError,
    SecretCipher,
    SecretContext,
    SecretField,
    UnknownKeyError,
)
from kaspisa.security.cipher import FORMAT_VERSION

if TYPE_CHECKING:
    from kaspisa.db.database import Database


@dataclass(frozen=True)
class ClaimedShop:
    """Захваченный магазин: адрес для дальнейшей работы в области арендатора."""

    tenant_id: uuid.UUID
    shop_id: uuid.UUID


@dataclass(frozen=True)
class HaltNotice:
    """Остановка бота, о которой владельцу ещё не сообщено окончательно."""

    tenant_id: uuid.UUID
    shop_id: uuid.UUID
    merchant_uid: str | None
    reason: str
    halted_at: datetime


@dataclass(frozen=True)
class StockResetNotice:
    """Товар, выключенный из-за сброса остатка, о котором владельцу ещё не сообщено."""

    tenant_id: uuid.UUID
    product_id: int
    title: str
    points: tuple[str, ...]
    tracked: bool
    reset_at: datetime


@dataclass(frozen=True)
class UnreadableSecret:
    """Блок, который не расшифровывается: только адрес, без значений."""

    tenant_id: uuid.UUID
    shop_id: uuid.UUID
    field: SecretField


@dataclass(frozen=True)
class SecretsReport:
    rotated: int  # перешифровано в этом запуске (в режиме проверки 0)
    blocks_by_key: Mapping[int, int]  # по байту идентификатора ключа в заголовке
    pending: int  # читаются, но не на основном ключе
    unreadable: Sequence[UnreadableSecret]  # битые блоки: ключ в связке есть, расшифровки нет
    # Блоки с идентификатором ключа, которого нет в связке: ошибка настройки, а не порча.
    unknown_key: Sequence[UnreadableSecret] = ()
    unknown_key_ids: Sequence[int] = ()  # эти идентификаторы (не секрет), по возрастанию


_BLOCK_COLUMNS: tuple[tuple[str, SecretField], ...] = (
    ("login_enc", SecretField.SHOP_LOGIN),
    ("password_enc", SecretField.SHOP_PASSWORD),
    ("session_enc", SecretField.SHOP_SESSION),
)


_BLOCK_COLUMN_NAMES = tuple(column for column, _ in _BLOCK_COLUMNS)

# Ключ advisory-блокировки создания первой учётной записи (произвольная константа bigint).
FIRST_ACCOUNT_LOCK: Final = 0x6B61_7370_0001

# Срок между загрузками каталога обработчиком (ADR 0010, Н7).
CATALOG_REFRESH_SECONDS = 86_400

# Захват: один UPDATE по строке, выбранной с SKIP LOCKED; условие аренды повторено снаружи.
_CLAIM_SQL = text(
    """
    UPDATE shop SET claimed_by = :worker, claimed_until = :until
    WHERE id = (
        SELECT s.id FROM shop s
        CROSS JOIN LATERAL (
            SELECT least(
                (SELECT min(coalesce(p.last_checked_at, CAST('-infinity' AS timestamptz)))
                   FROM product p
                  WHERE p.tenant_id = s.tenant_id AND p.shop_id = s.id
                    AND p.tracked AND s.bot_mode <> 'off' AND s.halt_reason IS NULL
                    AND s.city_id IS NOT NULL
                    AND (p.retry_at IS NULL OR p.retry_at <= :now)
                    AND (
                         (p.min_price IS NOT NULL AND p.max_price IS NOT NULL
                          AND (p.last_checked_at IS NULL OR p.last_checked_at <= :cutoff))
                      OR ((p.min_price IS NULL OR p.max_price IS NULL)
                          AND (p.last_checked_at IS NULL
                               OR p.last_checked_at <= :unbounded_cutoff))
                    )),
                (SELECT min(w.next_verify_at) FROM price_write w
                  WHERE w.tenant_id = s.tenant_id AND w.shop_id = s.id
                    AND w.next_verify_at <= :now),
                -- Каталог устарел: событие «сейчас», то есть позже любой проверки цен, которой
                -- пора (такие события старше `now`): магазины с загрузкой не вытесняют проверки.
                (SELECT :now WHERE s.connection_state = 'connected'
                    AND (s.halt_reason IS NULL OR s.halt_reason <> :login_halt)
                    AND (s.catalog_synced_at IS NULL OR s.catalog_synced_at <= :catalog_cutoff)
                    AND (s.catalog_retry_at IS NULL OR s.catalog_retry_at <= :now))
            ) AS event_at
        ) e
        WHERE (s.claimed_until IS NULL OR s.claimed_until <= :now)
          AND s.merchant_uid IS NOT NULL
          AND (s.connection_state = 'connected'
               OR (s.connection_state = 'reauth_required'
                   AND s.connection_error = 'session_expired'
                   AND s.bot_mode <> 'off' AND s.halt_reason IS NULL))
          AND e.event_at IS NOT NULL
        ORDER BY e.event_at, s.id
        LIMIT 1
        FOR UPDATE OF s SKIP LOCKED
    )
    AND (claimed_until IS NULL OR claimed_until <= :now)
    RETURNING tenant_id, id
    """
)


def _key_byte(column: str) -> str:
    """Идентификатор ключа блока (второй байт) или NULL; значение блока не выбирается."""
    return (
        f"CASE WHEN length({column}) >= 2 AND get_byte({column}, 0) = :fv "
        f"THEN get_byte({column}, 1) END"
    )


def _is_unknown_key(cipher: SecretCipher, key_id: int) -> bool:
    """Есть ли ключа в связке: проба расшифровки заведомо пустого блока с таким заголовком."""
    probe = bytes([FORMAT_VERSION, key_id]) + bytes(64)
    context = SecretContext(uuid.UUID(int=0), uuid.UUID(int=0), SecretField.SHOP_LOGIN)
    try:
        cipher.decrypt(probe, context)
    except UnknownKeyError:
        return True
    except DecryptionError:
        return False
    return False


@dataclass
class _Tally:
    """Накопитель обхода перешифрования."""

    rotated: int = 0
    pending: int = 0
    by_key: Counter[int] = field(default_factory=Counter)
    unreadable: list[UnreadableSecret] = field(default_factory=list)
    unknown_key: list[UnreadableSecret] = field(default_factory=list)
    unknown_key_ids: set[int] = field(default_factory=set)


class SystemScope:
    """Именованные запросы без арендатора; произвольных запросов область не принимает."""

    def __init__(self, session: AsyncSession, cipher_provider: Callable[[], SecretCipher]) -> None:
        self._session = session
        self._cipher_provider = cipher_provider

    async def create_tenant(self, *, name: str) -> Tenant:
        tenant = Tenant(name=name)
        self._session.add(tenant)
        await run_guarded(self._session, self._session.flush)
        return tenant

    async def find_user_by_email(self, email: str) -> User | None:
        """Вход: арендатор ещё не известен."""
        stmt = select(User).where(User.email == normalize_email(email))
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def has_users(self) -> bool:
        """Есть ли хоть один пользователь на установке: дешёвая проверка без блокировки."""
        return await self._users_exist()

    async def _users_exist(self) -> bool:
        stmt = select(exists().where(User.id.is_not(None)))
        return bool((await self._session.execute(stmt)).scalar_one())

    async def register_first(self, *, email: str, password_hash: str) -> User | None:
        """Создаёт арендатора и пользователя, только если на установке нет пользователей.

        Проверка и вставка идут в одной транзакции под `pg_advisory_xact_lock`: две
        одновременные первые регистрации не создадут две учётные записи (ADR 0027).
        Блокировка снимается вместе с транзакцией. `None` - установка уже не пуста.
        """
        address = normalize_email(email)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),
            {"key": FIRST_ACCOUNT_LOCK},
        )
        if await self._users_exist():
            await self._session.rollback()
            return None
        tenant = Tenant(name=address)
        self._session.add(tenant)
        await run_guarded(self._session, self._session.flush)
        user = User(tenant_id=tenant.id, email=address, password_hash=password_hash, is_admin=True)
        self._session.add(user)
        await run_guarded(self._session, self._session.flush)
        await run_guarded(self._session, self._session.commit)
        return user

    async def commit(self) -> None:
        await run_guarded(self._session, self._session.commit)

    async def claim_shop(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
        recheck_seconds: int,
        unbounded_recheck_seconds: int,
        catalog_refresh_seconds: int = CATALOG_REFRESH_SECONDS,
    ) -> ClaimedShop | None:
        """Захватывает магазин арендой одним оператором; None, если брать нечего.

        Магазин нужен, если есть товар «пора проверять» (бот включён, нет остановки, задан
        город; при режиме «выключен» не проверяется ни один товар, ADR 0031) или открытая
        запись, чью проверку пора делать, либо устарел каталог (`connected`, нет остановки по
        входу, нет паузы после сбоя; бот и город не нужны). Порядок: по самой давней дате из
        этих событий (товар без проверок первым); каталог считается событием «сейчас» и идёт
        после проверок цен.
        Аренда фиксируется сразу (commit внутри метода).
        """
        stmt = _CLAIM_SQL.bindparams(
            bindparam("now", now, type_=DateTime(timezone=True)),
            bindparam(
                "cutoff", now - timedelta(seconds=recheck_seconds), type_=DateTime(timezone=True)
            ),
            bindparam(
                "unbounded_cutoff",
                now - timedelta(seconds=unbounded_recheck_seconds),
                type_=DateTime(timezone=True),
            ),
            bindparam(
                "until", now + timedelta(seconds=lease_seconds), type_=DateTime(timezone=True)
            ),
            bindparam("worker", worker_id),
            bindparam(
                "catalog_cutoff",
                now - timedelta(seconds=catalog_refresh_seconds),
                type_=DateTime(timezone=True),
            ),
            bindparam("login_halt", LOGIN_HALT_REASON),
        )
        row = (await run_guarded(self._session, lambda: self._session.execute(stmt))).first()
        await self.commit()
        if row is None:
            return None
        return ClaimedShop(tenant_id=row.tenant_id, shop_id=row.id)

    async def unknown_key_spread(self, shop_id: uuid.UUID) -> int:
        """Сколько магазинов имеют блок с тем же неизвестным идентификатором ключа.

        Читаются только два байта заголовка блоков (версия и идентификатор ключа), значения
        блоков не читаются и не расшифровываются. 0, если у магазина нет блоков на
        неизвестном ключе; иначе число магазинов (включая его) с такими же блоками.
        """
        # Имена столбцов — константы модуля, значений пользователя в тексте запроса нет.
        columns = ", ".join(f"{_key_byte(col)} AS {col}" for col in _BLOCK_COLUMN_NAMES)
        header_sql = f"SELECT {columns} FROM shop WHERE id = :id"  # noqa: S608
        header_stmt = text(header_sql).bindparams(
            bindparam("id", shop_id), bindparam("fv", FORMAT_VERSION)
        )
        row = (await self._session.execute(header_stmt)).one_or_none()
        if row is None:
            return 0
        cipher = self._cipher_provider()
        unknown = sorted({k for k in row if k is not None and _is_unknown_key(cipher, k)})
        if not unknown:
            return 0
        any_block = " OR ".join(
            f"{_key_byte(col)} = ANY(CAST(:ids AS int[]))" for col in _BLOCK_COLUMN_NAMES
        )
        count_sql = f"SELECT count(*) FROM shop WHERE {any_block}"  # noqa: S608
        count_stmt = text(count_sql).bindparams(
            bindparam("ids", unknown, type_=ARRAY(Integer)), bindparam("fv", FORMAT_VERSION)
        )
        return int((await self._session.execute(count_stmt)).scalar_one())

    async def _scan_shop_batch(
        self, tally: _Tally, *, after: uuid.UUID | None, limit: int, write: bool
    ) -> uuid.UUID | None:
        """Одна пачка магазинов по id после `after`; возвращает последний id или None."""
        cipher = self._cipher_provider()
        stmt = select(Shop).order_by(Shop.id).limit(limit)
        if after is not None:
            stmt = stmt.where(Shop.id > after)
        if write:
            stmt = stmt.with_for_update()
        shops = (await self._session.execute(stmt)).scalars().all()
        if not shops:
            return None
        for shop in shops:
            for column, field_ in _BLOCK_COLUMNS:
                blob: bytes | None = getattr(shop, column)
                if blob is None:
                    continue
                context = SecretContext(shop.tenant_id, shop.id, field_)
                address = UnreadableSecret(shop.tenant_id, shop.id, field_)
                new_blob = _scan_block(tally, cipher, blob, context, address, write=write)
                if new_blob is not None:
                    setattr(shop, column, new_blob)
        if write:
            await self.commit()
        self._session.expunge_all()
        return shops[-1].id

    async def _scan_telegram_tokens(self, tally: _Tally, *, write: bool) -> None:
        """Токены Telegram одной пачкой (строк по числу арендаторов, не больше)."""
        cipher = self._cipher_provider()
        stmt = select(TelegramSettings).order_by(TelegramSettings.tenant_id)
        if write:
            stmt = stmt.with_for_update()
        for row in (await self._session.execute(stmt)).scalars().all():
            context = SecretContext(row.tenant_id, NO_SHOP, SecretField.TELEGRAM_TOKEN)
            address = UnreadableSecret(row.tenant_id, NO_SHOP, SecretField.TELEGRAM_TOKEN)
            new_blob = _scan_block(tally, cipher, row.token_enc, context, address, write=write)
            if new_blob is not None:
                row.token_enc = new_blob
        if write:
            await self.commit()
        self._session.expunge_all()

    async def pending_halt_notices(self, limit: int) -> list[HaltNotice]:
        """Остановки без окончательного исхода уведомления, самые ранние первыми.

        Строки не блокируются: отправка долгая, держать блокировку `shop` нельзя.
        """
        stmt = (
            select(Shop.tenant_id, Shop.id, Shop.merchant_uid, Shop.halt_reason, Shop.halted_at)
            .where(
                Shop.halt_reason.is_not(None),
                Shop.halt_notified_at.is_distinct_from(Shop.halted_at),
            )
            .order_by(Shop.halted_at, Shop.id)
            .limit(limit)
        )
        notices: list[HaltNotice] = []
        for tenant_id, shop_id, merchant_uid, reason, halted_at in await self._session.execute(
            stmt
        ):
            if reason is None or halted_at is None:
                continue
            notices.append(HaltNotice(tenant_id, shop_id, merchant_uid, reason, halted_at))
        return notices

    async def mark_halt_notified(self, shop_id: uuid.UUID, halted_at: datetime) -> bool:
        """Ставит отметку, только если остановка та же и ещё не отмечена."""
        stmt = (
            update(Shop)
            .where(
                Shop.id == shop_id,
                Shop.halted_at == halted_at,
                Shop.halt_notified_at.is_distinct_from(halted_at),
            )
            .values(halt_notified_at=halted_at)
            .returning(Shop.id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None

    async def pending_stock_reset_notices(self) -> list[StockResetNotice]:
        """У каждого арендатора самый ранний сброс остатка без окончательного исхода (ADR 0038).

        Единственная выборка данных товаров без фильтра арендатора: читает название и точки.
        Строки не блокируются.
        """
        stmt = (
            select(
                Product.tenant_id,
                Product.id,
                Product.title,
                Product.stock_reset_points,
                Product.tracked,
                Product.stock_reset_at,
            )
            .where(Product.stock_reset_at.is_not(None))
            .ext(distinct_on(Product.tenant_id))
            .order_by(Product.tenant_id, Product.stock_reset_at, Product.id)
        )
        notices: list[StockResetNotice] = []
        for tenant_id, product_id, title, points, tracked, reset_at in await self._session.execute(
            stmt
        ):
            if points is None or reset_at is None:
                continue
            notices.append(
                StockResetNotice(
                    tenant_id, product_id, title, tuple(points.split(",")), tracked, reset_at
                )
            )
        return notices

    async def mark_stock_reset_notified(
        self, tenant_id: TenantId, product_id: int, reset_at: datetime
    ) -> bool:
        """Снимает сброс, только если время то же: новый сброс не теряется."""
        stmt = (
            update(Product)
            .where(
                Product.tenant_id == tenant_id,
                Product.id == product_id,
                Product.stock_reset_at == reset_at,
            )
            .values(stock_reset_at=None, stock_reset_points=None)
            .returning(Product.id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None

    async def pending_version_tenants(self, version: str) -> list[TenantId]:
        """Арендаторы с настройками Telegram, которым о версии ещё не сообщено."""
        stmt = select(TelegramSettings.tenant_id).where(
            TelegramSettings.notified_version.is_distinct_from(version)
        )
        return [TenantId(t) for t in (await self._session.execute(stmt)).scalars().all()]

    async def mark_version_notified(self, tenant_id: TenantId, version: str) -> bool:
        stmt = (
            update(TelegramSettings)
            .where(
                TelegramSettings.tenant_id == tenant_id,
                TelegramSettings.notified_version.is_distinct_from(version),
            )
            .values(notified_version=version)
            .returning(TelegramSettings.tenant_id)
        )
        result = await run_guarded(self._session, lambda: self._session.execute(stmt))
        return result.first() is not None


def _scan_block(
    tally: _Tally,
    cipher: SecretCipher,
    blob: bytes,
    context: SecretContext,
    address: UnreadableSecret,
    *,
    write: bool,
) -> bytes | None:
    """Считает один блок; при `write` возвращает перешифрованный блок, иначе None."""
    try:
        key_id = cipher.key_id(blob)
        # Расшифровка проверяется и у блоков на основном ключе (без записи).
        cipher.decrypt(blob, context)
        if not cipher.needs_rotation(blob):
            tally.by_key[key_id] += 1
        elif write:
            new_blob = cipher.rotate(blob, context)
            tally.by_key[cipher.key_id(new_blob)] += 1
            tally.rotated += 1
            return new_blob
        else:
            tally.by_key[key_id] += 1
            tally.pending += 1
    except UnknownKeyError:
        tally.unknown_key.append(address)
        tally.unknown_key_ids.add(blob[1])
        tally.by_key[blob[1]] += 1
    except DecryptionError:
        tally.unreadable.append(address)
        if len(blob) >= 2:
            tally.by_key[blob[1]] += 1
    return None


async def _scan_shop_secrets(database: Database, *, batch_size: int, write: bool) -> SecretsReport:
    """Обход всех магазинов пачками по id; нечитаемый блок не прерывает обход и не меняется."""
    if batch_size < 1:
        raise ValueError("batch_size должен быть не меньше 1")
    tally = _Tally()
    last_id: uuid.UUID | None = None
    while True:
        async with database.system() as scope:
            last_id = await scope._scan_shop_batch(
                tally, after=last_id, limit=batch_size, write=write
            )
        if last_id is None:
            break
    async with database.system() as scope:
        await scope._scan_telegram_tokens(tally, write=write)
    return SecretsReport(
        rotated=tally.rotated,
        blocks_by_key=dict(sorted(tally.by_key.items())),
        pending=tally.pending,
        unreadable=tally.unreadable,
        unknown_key=tally.unknown_key,
        unknown_key_ids=sorted(tally.unknown_key_ids),
    )


async def reencrypt_shop_secrets(database: Database, *, batch_size: int = 100) -> SecretsReport:
    """Перешифровать блоки основным ключом; повторный запуск безопасен."""
    return await _scan_shop_secrets(database, batch_size=batch_size, write=True)


async def check_shop_secrets(database: Database, *, batch_size: int = 100) -> SecretsReport:
    """Тот же обход без записи: считает блоки, ждущие перешифрования."""
    return await _scan_shop_secrets(database, batch_size=batch_size, write=False)
