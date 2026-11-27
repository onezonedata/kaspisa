"""Модели ответов Kaspi. Разбираются только нужные поля; неизвестные игнорируются."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kaspisa.kaspi.errors import ResponseFormatError, SessionStateError
from kaspisa.kaspi.session import KaspiSession, SessionPhase
from kaspisa.security import Secret


class _Model(BaseModel):
    """Строгая неизменяемая модель с псевдонимами camelCase."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore", populate_by_name=True)


# --- продавцы карточки ---


class Offer(_Model):
    merchant_id: str = Field(alias="merchantId")
    merchant_name: str = Field(alias="merchantName")
    price: int = Field(gt=0)
    preorder: int | None = None

    @field_validator("price", mode="before")
    @classmethod
    def _integral_float_price(cls, value: Any) -> Any:
        """Сервер Kaspi отдаёт цену как `2998.0`; float с целым значением становится `int`.

        Остальное (дробь, NaN, бесконечность, bool, строка) проходит дальше и отклоняется
        строгой моделью.
        """
        if type(value) is float and value.is_integer():
            return int(value)
        return value


class OffersPage(_Model):
    offers: list[Offer]
    total: int


# --- список товаров кабинета ---


class ListedCityPrice(_Model):
    city_id: str = Field(alias="cityId")
    value: int


class ListedAvailability(_Model):
    store_id: str = Field(alias="storeId")
    available: Literal["yes", "no"]
    stock_count: int | None = Field(alias="stockCount", ge=0)
    stock_specified: bool = Field(alias="stockSpecified")
    pre_order: int | None = Field(alias="preOrder")


class ListedProduct(_Model):
    sku: str
    master_sku: str = Field(alias="masterSku")
    merchant_uid: str = Field(alias="merchantUid")
    master_title: str | None = Field(default=None, alias="masterTitle")
    available: bool
    archived_at: str | None = Field(default=None, alias="archivedAt")
    city_prices: list[ListedCityPrice] = Field(alias="cityPrices")
    availabilities: list[ListedAvailability]


class ProductListPage(_Model):
    data: list[ListedProduct]
    total: int


# --- чтение товара ---


class ProductPoint(_Model):
    id: str
    stock: int | None = Field(ge=0)


class ProductCity(_Model):
    id: str
    price: int = Field(gt=0)
    points: list[ProductPoint]


class ProductState(_Model):
    sku: str
    master_sku: str = Field(alias="masterSku")
    title: str | None
    available: bool
    cities: list[ProductCity] = Field(min_length=1)


# --- запись цены ---


@dataclass(frozen=True)
class ProductSnapshot:
    """Свежее чтение товара и строка списка товаров: вместе они дают тело записи."""

    state: ProductState
    listing: ListedProduct


class PriceUpdateAvailability(_Model):
    """Точка в теле записи; при `stock_count is None` ключ `stockCount` не сериализуется."""

    available: Literal["yes", "no"]
    store_id: str = Field(alias="storeId")
    stock_count: int | None = Field(default=None, alias="stockCount")


class PriceUpdateCityPrice(_Model):
    city_id: str = Field(alias="cityId")
    value: int


class PriceUpdateRequest(_Model):
    """Тело записи цены: ровно поля `price-update-request.json`.

    `model` обязателен и может быть `None`: тогда в JSON `"model": null` (ADR 0019).
    """

    merchant_uid: str = Field(alias="merchantUid")
    availabilities: list[PriceUpdateAvailability]
    city_prices: list[PriceUpdateCityPrice] = Field(alias="cityPrices")
    sku: str
    model: str | None

    def to_json_body(self) -> dict[str, Any]:
        """JSON запроса: `stockCount` пропускается при `None`, ключ `model` есть всегда."""
        body: dict[str, Any] = self.model_dump(by_alias=True, mode="json", exclude_none=True)
        body["model"] = self.model
        return body


class PriceWriteReceipt(_Model):
    """Номер задания записи цены (это не подтверждение применения)."""

    job_id: str = Field(alias="id")


@dataclass(frozen=True)
class PriceVerification:
    """Итог одной проверки записи по строке списка.

    `listing` - найденная строка (`None`, если не найдена); `mismatches` - город: (ожидали,
    в списке); `store_anomalies` - идентификаторы точек по возрастанию, пусто при `applied=False`.
    """

    applied: bool
    listing: ListedProduct | None
    mismatches: dict[str, tuple[int, int | None]]
    store_anomalies: tuple[str, ...]


# --- вход (не проверен на живом Kaspi: описан только по коду страницы) ---


class Merchant(_Model):
    uid: str
    roles: list[str] = Field(default_factory=list)


class MerchantsResponse(_Model):
    merchants: list[Merchant]


@dataclass(frozen=True)
class KaspiCredentials:
    """Логин и пароль кабинета. Оба - `Secret`: `repr` показывает только `***`."""

    login: Secret
    password: Secret


@dataclass(frozen=True)
class LoggedIn:
    """Вход выполнен: сессия в фазе `AUTHENTICATED` и магазины кабинета."""

    session: KaspiSession
    merchants: list[Merchant]

    def __post_init__(self) -> None:
        if self.session.phase is not SessionPhase.AUTHENTICATED:
            raise SessionStateError("LoggedIn требует сессию в фазе authenticated")


@dataclass(frozen=True)
class CodeRequired:
    """Kaspi прислал код на почту: сессия в фазе `AWAITING_CODE`. Это результат, а не ошибка."""

    session: KaspiSession

    def __post_init__(self) -> None:
        if self.session.phase is not SessionPhase.AWAITING_CODE:
            raise SessionStateError("CodeRequired требует сессию в фазе awaiting_code")


def _format_loc(loc: tuple[int | str, ...]) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else part
    return path or "<root>"


def parse_model[M: BaseModel](model: type[M], data: Any, operation: str) -> M:
    """Разбирает данные моделью; ошибка превращается в `ResponseFormatError` с путями полей.

    Значения из ответа в исключение не попадают, цепочки исключений нет.
    """
    fields: tuple[str, ...] | None = None
    result: M | None = None
    try:
        result = model.model_validate(data)
    except ValidationError as exc:
        fields = tuple(dict.fromkeys(_format_loc(tuple(err["loc"])) for err in exc.errors()))
    if result is None:
        raise ResponseFormatError(operation, fields or ())
    return result
