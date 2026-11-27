"""Модели данных. Расшифрованных значений и relationship() в моделях нет."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kaspisa.db.base import Base
from kaspisa.pricing.types import Strategy


class ShopConnectionState(StrEnum):
    """Состояние подключения магазина к Kaspi."""

    NOT_CONNECTED = "not_connected"
    AWAITING_CODE = "awaiting_code"
    CONNECTED = "connected"
    REAUTH_REQUIRED = "reauth_required"


class BotMode(StrEnum):
    """Режим бота магазина (ADR 0024): выключен, проверка без записи, работает."""

    OFF = "off"
    DRY_RUN = "dry_run"
    LIVE = "live"


class OwnerEventKind(StrEnum):
    """Вид события журнала действий владельца (ADR 0024)."""

    LOGIN = "login"
    PASSWORD_CONFIRM_FAILED = "password_confirm_failed"  # noqa: S105 (код события, не пароль)
    BOT_MODE_CHANGED = "bot_mode_changed"
    BOUNDS_CHANGED = "bounds_changed"
    USER_ADDED = "user_added"
    USER_REMOVED = "user_removed"


class PriceChangeResult(StrEnum):
    """Чем закончилась запись цены."""

    APPLIED = "applied"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"
    # решение «не менять»: строка журнала с `new_price = old_price` (ADR 0020)
    NO_CHANGE = "no_change"


class SkipReason(StrEnum):
    """Почему последняя проверка товара пропущена (ADR 0017); в базе только код."""

    LISTING_MISSING = "listing_missing"
    OWN_PRICE_UNKNOWN = "own_price_unknown"
    ENGINE_REJECTED = "engine_rejected"
    RESPONSE_FORMAT = "response_format"
    KASPI_ERROR = "kaspi_error"


class PriceWriteStatus(StrEnum):
    """Состояние отправленной записи цены (запрет и подтверждение)."""

    SENDING = "sending"
    RECEIPT = "receipt"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"


def _in_list(column: str, values: type[StrEnum]) -> str:
    items = ", ".join(f"'{v.value}'" for v in values)
    return f"{column} IN ({items})"


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"Tenant(id={self.id})"


class User(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        UniqueConstraint("email", name="uq_app_user_email"),
        Index("ix_app_user_tenant_id", "tenant_id"),
        # Администратор в арендаторе не больше одного (ADR 0039).
        Index(
            "uq_app_user_tenant_id_admin",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_admin"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="CASCADE", name="fk_app_user_tenant_id_tenant")
    )
    email: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    # Роль ставит только `register_first`; API смены роли нет (ADR 0039).
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"User(id={self.id}, tenant_id={self.tenant_id})"


DEFAULT_CITY_ID = "710000000"  # Астана: город сверки нового магазина (ADR 0032)


class Shop(Base):
    __tablename__ = "shop"
    __table_args__ = (
        CheckConstraint(_in_list("connection_state", ShopConnectionState), name="connection_state"),
        CheckConstraint(_in_list("strategy", Strategy), name="strategy"),
        CheckConstraint("default_step >= 1", name="default_step_positive"),
        CheckConstraint(_in_list("bot_mode", BotMode), name="bot_mode"),
        CheckConstraint(
            "connection_state <> 'connected' "
            "OR (merchant_uid IS NOT NULL AND session_enc IS NOT NULL)",
            name="connected_requires_session",
        ),
        CheckConstraint("(halt_reason IS NULL) = (halted_at IS NULL)", name="halt_both_or_none"),
        CheckConstraint("login_error_streak >= 0", name="login_error_streak_not_negative"),
        CheckConstraint("write_error_streak >= 0", name="write_error_streak_not_negative"),
        UniqueConstraint("tenant_id", "id", name="uq_shop_tenant_id_id"),
        Index("ix_shop_claimed_until", "claimed_until"),
        Index(
            "uq_shop_merchant_uid",
            "merchant_uid",
            unique=True,
            postgresql_where=text("merchant_uid IS NOT NULL"),
        ),
        Index("ix_shop_tenant_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="CASCADE", name="fk_shop_tenant_id_tenant")
    )
    merchant_uid: Mapped[str | None] = mapped_column(Text)
    login_enc: Mapped[bytes] = mapped_column(LargeBinary)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary)
    session_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    connection_state: Mapped[str] = mapped_column(
        String(32), default=ShopConnectionState.NOT_CONNECTED.value, server_default="not_connected"
    )
    connection_error: Mapped[str | None] = mapped_column(Text)
    connection_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    connection_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    strategy: Mapped[str] = mapped_column(
        String(32), default=Strategy.BE_FIRST.value, server_default="be_first"
    )
    city_id: Mapped[str | None] = mapped_column(Text)
    excluded_merchant_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    default_step: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    auto_raise_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    bot_mode: Mapped[str] = mapped_column(
        String(16), default=BotMode.OFF.value, server_default="off"
    )
    # Аренда магазина обработчиком и срок пропуска (см. ADR 0008).
    claimed_by: Mapped[str | None] = mapped_column(Text)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Остановка бота магазина: снимает только человек.
    halt_reason: Mapped[str | None] = mapped_column(Text)
    halted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # `halted_at` той остановки, по которой исход уведомления окончательный (ADR 0025).
    halt_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    login_error_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    write_error_streak: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Версия состояния, созданная автовходом и ещё не подтверждённая запросом кабинета.
    unproven_login_version: Mapped[int | None] = mapped_column(Integer)
    # Загрузка каталога: последняя успешная (кнопка или обработчик) и пауза после сбоя.
    catalog_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catalog_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"Shop(id={self.id}, tenant_id={self.tenant_id}, "
            f"merchant_uid={self.merchant_uid!r}, connection_state={self.connection_state!r})"
        )


class Product(Base):
    __tablename__ = "product"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "shop_id"],
            ["shop.tenant_id", "shop.id"],
            ondelete="CASCADE",
            name="fk_product_tenant_id_shop_id_shop",
        ),
        UniqueConstraint("shop_id", "sku", name="uq_product_shop_id_sku"),
        UniqueConstraint("tenant_id", "id", name="uq_product_tenant_id_id"),
        CheckConstraint("min_price IS NULL OR min_price >= 1", name="min_price_positive"),
        CheckConstraint(
            "min_price_anchor IS NULL OR min_price_anchor >= 1", name="min_price_anchor_positive"
        ),
        CheckConstraint("max_price IS NULL OR max_price >= 1", name="max_price_positive"),
        CheckConstraint(
            "min_price IS NULL OR max_price IS NULL OR min_price <= max_price",
            name="min_not_above_max",
        ),
        CheckConstraint("step >= 1", name="step_positive"),
        CheckConstraint(
            "current_price IS NULL OR current_price >= 1", name="current_price_positive"
        ),
        CheckConstraint("position IS NULL OR position >= 1", name="position_positive"),
        CheckConstraint(
            "position_over IS NULL OR position_over >= 1", name="position_over_positive"
        ),
        CheckConstraint("position IS NULL OR position_over IS NULL", name="position_or_over"),
        CheckConstraint(
            "competitors_count IS NULL OR competitors_count >= 0",
            name="competitors_count_not_negative",
        ),
        CheckConstraint(
            "first_place_price IS NULL OR first_place_price >= 1", name="first_place_price_positive"
        ),
        CheckConstraint(
            "(skip_reason IS NULL) = (skipped_at IS NULL)", name="skip_reason_with_time"
        ),
        CheckConstraint("skip_reason IS NULL OR decision_reason IS NULL", name="skip_or_decision"),
        CheckConstraint(
            "decision_target_price IS NULL OR decision_reason IS NOT NULL",
            name="decision_target_with_reason",
        ),
        CheckConstraint(
            "decision_write IS NULL OR decision_reason IS NOT NULL",
            name="decision_write_with_reason",
        ),
        CheckConstraint(
            "decision_target_price IS NULL OR decision_target_price >= 1",
            name="decision_target_price_positive",
        ),
        CheckConstraint(
            "(stock_reset_at IS NULL) = (stock_reset_points IS NULL)",
            name="stock_reset_with_points",
        ),
        Index(
            "ix_product_shop_id_last_checked_at",
            "shop_id",
            "last_checked_at",
            postgresql_where=text("tracked"),
        ),
        Index(
            "ix_product_stock_reset_at",
            "tenant_id",
            "stock_reset_at",
            "id",
            postgresql_where=text("stock_reset_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    shop_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sku: Mapped[str] = mapped_column(Text)
    master_sku: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    auto_decrease: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    auto_raise: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    min_price: Mapped[int | None] = mapped_column(Integer)
    max_price: Mapped[int | None] = mapped_column(Integer)
    # Якорь минимума (ADR 0024): от него меряется «опасное» снижение; без пароля только растёт.
    min_price_anchor: Mapped[int | None] = mapped_column(Integer)
    step: Mapped[int] = mapped_column(Integer)
    current_price: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int | None] = mapped_column(Integer)
    # Своего магазина нет среди N прочитанных продавцов карточки, а их больше: «N+» (ADR 0016).
    position_over: Mapped[int | None] = mapped_column(Integer)
    competitors_count: Mapped[int | None] = mapped_column(Integer)
    first_place_price: Mapped[int | None] = mapped_column(Integer)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Причина пропуска последней проверки (код `SkipReason`) и время этой проверки.
    skip_reason: Mapped[str | None] = mapped_column(Text)
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Решение последней проверки (ADR 0020): код причины, цель и итог записи для «менять».
    # Взаимоисключающе с `skip_reason`.
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decision_target_price: Mapped[int | None] = mapped_column(Integer)
    decision_write: Mapped[str | None] = mapped_column(Text)
    # Отслеживается ли товар ботом (по умолчанию нет).
    tracked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Бот выключил товар из-за сброса остатка (ADR 0038): время и точки через запятую.
    # Пусто, когда сообщать не о чем; окончательный исход уведомления очищает оба.
    stock_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stock_reset_points: Mapped[str | None] = mapped_column(Text)
    # Текущий отказ записи и серия временных отказов.
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    refusal_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refusal_fingerprint: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"Product(id={self.id}, tenant_id={self.tenant_id}, shop_id={self.shop_id})"


class PriceChange(Base):
    __tablename__ = "price_change"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "shop_id"],
            ["shop.tenant_id", "shop.id"],
            ondelete="CASCADE",
            name="fk_price_change_tenant_id_shop_id_shop",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["product.tenant_id", "product.id"],
            ondelete="CASCADE",
            name="fk_price_change_tenant_id_product_id_product",
        ),
        CheckConstraint("old_price >= 1", name="old_price_positive"),
        CheckConstraint("new_price >= 1", name="new_price_positive"),
        CheckConstraint("(result = 'no_change') = (new_price = old_price)", name="price_changed"),
        CheckConstraint("skipped_competitors >= 0", name="skipped_competitors_not_negative"),
        CheckConstraint(_in_list("result", PriceChangeResult), name="result"),
        UniqueConstraint("tenant_id", "id", name="uq_price_change_tenant_id_id"),
        Index("ix_price_change_tenant_id_shop_id_id", "tenant_id", "shop_id", text("id DESC")),
        Index(
            "ix_price_change_tenant_id_product_id_id", "tenant_id", "product_id", text("id DESC")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    shop_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    old_price: Mapped[int] = mapped_column(Integer)
    new_price: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    target_price: Mapped[int | None] = mapped_column(Integer)
    competitor_merchant_id: Mapped[str | None] = mapped_column(Text)
    competitor_name: Mapped[str | None] = mapped_column(Text)
    competitor_price: Mapped[int | None] = mapped_column(Integer)
    capped_at_max: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    skipped_competitors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # запись выровняла разные цены городов товара (ADR 0011, Н12)
    cities_aligned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    result: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return (
            f"PriceChange(id={self.id}, tenant_id={self.tenant_id}, product_id={self.product_id})"
        )


class PriceWrite(Base):
    """Запрет повторной записи и данные для подтверждения: одна строка на товар."""

    __tablename__ = "price_write"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["product.tenant_id", "product.id"],
            ondelete="CASCADE",
            name="fk_price_write_tenant_id_product_id_product",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "shop_id"],
            ["shop.tenant_id", "shop.id"],
            ondelete="CASCADE",
            name="fk_price_write_tenant_id_shop_id_shop",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "price_change_id"],
            ["price_change.tenant_id", "price_change.id"],
            name="fk_price_write_tenant_id_price_change_id_price_change",
        ),
        CheckConstraint(_in_list("status", PriceWriteStatus), name="status"),
        Index(
            "ix_price_write_tenant_id_shop_id_next_verify_at",
            "tenant_id",
            "shop_id",
            "next_verify_at",
        ),
    )

    product_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    shop_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    sku: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    expected_prices: Mapped[dict[str, int]] = mapped_column(JSONB)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    job_id: Mapped[str | None] = mapped_column(Text)
    next_verify_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_reported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # ошибка записи уже учтена в серии магазина (при отправке или на сроке 300 с)
    error_counted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    price_change_id: Mapped[int] = mapped_column(BigInteger)

    def __repr__(self) -> str:
        return f"PriceWrite(product_id={self.product_id}, tenant_id={self.tenant_id})"


class OwnerEvent(Base):
    """Журнал действий владельца: входы, неверные пароли, смена режима и границ (ADR 0024).

    Секретов нет по построению: ни пароля, ни токена, ни cookie. `shop_id` без внешнего ключа:
    событие переживает удаление магазина.
    """

    __tablename__ = "owner_event"
    __table_args__ = (
        CheckConstraint(_in_list("kind", OwnerEventKind), name="kind"),
        Index("ix_owner_event_tenant_id_created_at", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenant.id", ondelete="CASCADE", name="fk_owner_event_tenant_id_tenant")
    )
    # После удаления пользователя `user_id` обнуляется, автор остаётся по снимку адреса.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("app_user.id", ondelete="SET NULL", name="fk_owner_event_user_id_app_user")
    )
    user_email: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    ip: Mapped[str] = mapped_column(Text)
    shop_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sku: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"OwnerEvent(id={self.id}, tenant_id={self.tenant_id}, kind={self.kind!r})"


class TelegramSettings(Base):
    """Настройки уведомлений Telegram: одна строка на арендатора (ADR 0025).

    Токен хранится только зашифрованным; в `repr` нет ничего, кроме арендатора.
    """

    __tablename__ = "telegram_settings"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenant.id", ondelete="CASCADE", name="fk_telegram_settings_tenant_id_tenant"),
        primary_key=True,
    )
    token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    chat_id: Mapped[str] = mapped_column(Text)
    # Версия, о которой сообщение доставлено или признано недоставляемым.
    notified_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"TelegramSettings(tenant_id={self.tenant_id})"
