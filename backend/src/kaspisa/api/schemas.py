"""Модели Pydantic входа и выхода. Секретных полей в выходных моделях нет."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from kaspisa.db import BotMode, OwnerEventKind
from kaspisa.pricing.types import Strategy

PASSWORD_MIN = 10
PASSWORD_MAX = 128
PRICE_MAX = 100_000_000  # верхний предел цены и шага на входе
ID_MAX = 2**63 - 1  # предел bigint: больше база не принимает
BULK_MAX = 500  # предел товаров в массовой правке


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _normalize_email(value: object) -> object:
    if not isinstance(value, str):
        return value
    email = value.strip().lower()
    local, separator, domain = email.partition("@")
    if not 3 <= len(email) <= 254:
        raise ValueError("email length")
    if not separator or not local or not domain or "@" in domain or any(c.isspace() for c in email):
        raise ValueError("email format")
    return email


class RegisterIn(_In):
    email: str
    password: Annotated[
        str, StringConstraints(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX), Field(repr=False)
    ]

    _email = field_validator("email", mode="before")(_normalize_email)


class LoginIn(_In):
    email: str
    password: Annotated[
        str, StringConstraints(min_length=1, max_length=PASSWORD_MAX), Field(repr=False)
    ]

    _email = field_validator("email", mode="before")(_normalize_email)


class PasswordResetIn(_In):
    token: Annotated[str, StringConstraints(min_length=1, max_length=200), Field(repr=False)]
    new_password: Annotated[
        str, StringConstraints(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX), Field(repr=False)
    ]


class PasswordConfirmIn(_In):
    """Повторный ввод пароля сервиса перед опасной правкой (ADR 0024)."""

    password: Annotated[
        str, StringConstraints(min_length=1, max_length=PASSWORD_MAX), Field(repr=False)
    ]


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    is_admin: bool


class UserAddIn(_In):
    """Добавление пользователя администратором (ADR 0039)."""

    email: str

    _email = field_validator("email", mode="before")(_normalize_email)


class UserAdminOut(BaseModel):
    """Строка списка пользователей: без пароля и признаков входа."""

    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime


class UserAddedOut(BaseModel):
    """Ответ на добавление: токен задания пароля показывается один раз."""

    user: UserAdminOut
    reset_token: Annotated[str, Field(repr=False)]
    expires_in_seconds: int


class OwnerEventOut(BaseModel):
    """Событие журнала действий владельца: без паролей, токенов и cookie."""

    id: int
    kind: OwnerEventKind
    user_email: str
    ip: str
    shop_id: uuid.UUID | None
    sku: str | None
    old_value: str | None
    new_value: str | None
    created_at: datetime


class ErrorOut(BaseModel):
    code: str
    retry_after_seconds: int | None = None
    fields: list[str] | None = Field(default=None, description="Пути полей без значений (422)")


class ShopCredentialsIn(_In):
    login: Annotated[str, StringConstraints(min_length=1, max_length=254), Field(repr=False)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256), Field(repr=False)]

    @field_validator("login")
    @classmethod
    def _login_not_padded(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("login padding")
        return value


class ShopSettingsIn(_In):
    """Частичная правка: поле, которого нет в теле, не меняется (по `model_fields_set`)."""

    strategy: Strategy = Strategy.BE_FIRST
    city_id: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,32}$")] | None = None
    excluded_merchant_ids: Annotated[
        list[Annotated[str, StringConstraints(min_length=1, max_length=64)]],
        Field(max_length=200),
    ] = Field(default_factory=list)
    default_step: int = Field(default=1, ge=1, le=PRICE_MAX)
    auto_raise_enabled: bool = True
    bot_mode: BotMode = BotMode.OFF

    @field_validator("excluded_merchant_ids")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("empty body")
        return self


class ShopOut(BaseModel):
    id: uuid.UUID
    merchant_uid: str | None
    connection_state: str
    connection_error: str | None
    connection_changed_at: datetime
    resend_available_in_seconds: int | None
    restart_required: bool
    halt_reason: str | None
    halted_at: datetime | None
    strategy: str
    city_id: str | None
    excluded_merchant_ids: list[str]
    default_step: int
    auto_raise_enabled: bool
    bot_mode: BotMode
    # Флаг `KASPISA_DRY_RUN` обработчика: `True` - запись запрещена всему серверу, `False` -
    # обработчик пишет цены у магазинов в режиме `live`, `None` - обработчик не отвечает.
    server_dry_run: bool | None
    created_at: datetime
    catalog_synced_at: datetime | None
    # Идёт загрузка каталога (блокировка в Redis); у неподключённого магазина всегда False.
    catalog_loading: bool


class CatalogSummaryOut(BaseModel):
    """Число товаров магазина; оба `None`, если каталог больше предела."""

    total: int | None
    tracked: int | None


class CodeIn(_In):
    code: Annotated[str, StringConstraints(pattern=r"^[0-9]{4,12}$"), Field(repr=False)]


class ConnectResult(BaseModel):
    outcome: Literal[
        "connected",
        "code_required",
        "code_sent",
        "code_invalid",
        "rejected",
        "temporary_error",
        "restart_required",
    ]
    retry_after_seconds: int | None
    shop: ShopOut


class ProductSettingsIn(_In):
    """Частичная правка товара: поле, которого нет в теле, не меняется."""

    tracked: bool = False
    auto_decrease: bool = False
    auto_raise: bool = False
    min_price: Annotated[int, Field(ge=1, le=PRICE_MAX)] | None = None
    max_price: Annotated[int, Field(ge=1, le=PRICE_MAX)] | None = None
    step: int = Field(default=1, ge=1, le=PRICE_MAX)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("empty body")
        return self


class BulkChanges(_In):
    """Массово меняются только эти поля; границы цены - у каждого товара отдельно."""

    tracked: bool = False
    auto_decrease: bool = False
    auto_raise: bool = False
    step: int = Field(default=1, ge=1, le=PRICE_MAX)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("empty changes")
        return self


class BulkProductSettingsIn(_In):
    product_ids: Annotated[
        list[Annotated[int, Field(ge=1, le=ID_MAX)]], Field(min_length=1, max_length=BULK_MAX)
    ]
    changes: BulkChanges

    @field_validator("product_ids")
    @classmethod
    def _unique(cls, value: list[int]) -> list[int]:
        return sorted(set(value))


class PendingWrite(BaseModel):
    """Открытая запись цены: новая граница действует со следующей записи."""

    status: str
    sent_at: datetime
    hold_until: datetime
    prices: dict[str, int]
    outside_bounds: bool


class ProductOut(BaseModel):
    id: int
    shop_id: uuid.UUID
    sku: str
    master_sku: str
    title: str
    tracked: bool
    auto_decrease: bool
    auto_raise: bool
    min_price: int | None
    max_price: int | None
    step: int
    current_price: int | None
    position: int | None
    # Своего магазина нет среди N прочитанных продавцов, а их больше: интерфейс пишет «N+».
    position_over: int | None
    competitors_count: int | None
    first_place_price: int | None
    last_checked_at: datetime | None
    refusal_reason: str | None
    refusal_since: datetime | None
    retry_at: datetime | None
    # Код причины, по которой пропущена последняя проверка, и время этой проверки.
    skip_reason: str | None
    skipped_at: datetime | None
    # Решение последней проверки (ADR 0020): код причины, цель и итог записи для «менять».
    decision_reason: str | None
    decision_target_price: int | None
    decision_write: str | None
    pending_write: PendingWrite | None


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int


class BulkResult(BaseModel):
    updated: int


SyncOutcome = Literal["synced", "rejected", "temporary_error"]


class SyncResult(BaseModel):
    outcome: SyncOutcome
    added: int
    updated: int
    missing: int
    retry_after_seconds: int | None
    shop: ShopOut


class PriceChangeOut(BaseModel):
    id: int
    product_id: int
    sku: str
    title: str
    created_at: datetime
    old_price: int
    new_price: int
    reason: str
    target_price: int | None
    competitor_merchant_id: str | None
    competitor_name: str | None
    competitor_price: int | None
    capped_at_max: bool
    skipped_competitors: int
    cities_aligned: bool
    result: str
    error: str | None


class PriceChangePage(BaseModel):
    items: list[PriceChangeOut]
    limit: int
    offset: int


class VersionOut(BaseModel):
    current: str
    latest: str | None
    update_available: bool
    release_url: str | None
