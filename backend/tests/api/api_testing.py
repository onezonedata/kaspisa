"""Общие помощники тестов API: база, Redis, часы, заглушка клиента, HTTP-клиент, ROUTES.

Фикстуры не в conftest.py (второй conftest ломает mypy): тесты импортируют их явно.
Копия работы с тестовой базой из tests/worker/worker_testing.py: из другого каталога
тестов этот модуль не импортируется.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.config import Config
from api_kaspi_testing import load_fixture
from redis.asyncio import Redis
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic import command
from kaspisa.api import ApiDeps, create_app
from kaspisa.api.catalog_state import ApiSyncLock
from kaspisa.api.deps import SESSION_COOKIE
from kaspisa.api.passwords import hash_password
from kaspisa.api.redis_state import RedisState
from kaspisa.api.telegram import Notifier
from kaspisa.db import TenantId, create_database
from kaspisa.kaspi.models import (
    CodeRequired,
    KaspiCredentials,
    ListedProduct,
    LoggedIn,
    Merchant,
    OffersPage,
)
from kaspisa.kaspi.session import KaspiSession
from kaspisa.security import KeyRing, Secret, SecretCipher
from kaspisa.worker.redis_limits import LoginLock, PriceReadLock, RedisIntervalLimiter

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic" / "alembic.ini"
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "docs" / "fixtures" / "kaspi"

# Заметные секреты: тесты проверяют, что их нет в ответах и логах.
SERVICE_PASSWORD = "service-pass-7f3a"  # noqa: S105
KASPI_PASSWORD = "KASPI-PASS-7f3a"  # noqa: S105
KASPI_LOGIN = "seller-7f3a@shop.kz"
KASPI_COOKIE = "COOKIE-7f3a"
EMAIL_CODE = "482913"
CSRF_HEADERS = {"X-Requested-With": "kaspisa"}


class FakeClock:
    """Сдвигаемые часы; начальное значение - текущее время."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeConnectClient:
    """Заглушка `KaspiConnectApi`: очередь исходов по методу и запись вызовов.

    Исход - результат или исключение; пустая очередь - `AssertionError` (лишний вызов
    клиента). `before_return` выполняется перед ответом (побочный эффект в другой области),
    `mutate_session` меняет переданную сессию (`submit_code`, `resend_code`), `hang` - метод
    не возвращается никогда.
    """

    METHODS = ("login", "submit_code", "resend_code", "list_products", "get_offers")

    def __init__(self) -> None:
        self.outcomes: dict[str, list[Any]] = {name: [] for name in self.METHODS}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.before_return: Callable[[], Awaitable[None]] | None = None
        self.mutate_session: Callable[[KaspiSession], None] | None = None
        self.hang: set[str] = set()

    def queue(self, method: str, *outcomes: Any) -> None:
        self.outcomes[method].extend(outcomes)

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == method]

    async def _run(self, method: str, **args: Any) -> Any:
        self.calls.append((method, args))
        if method in self.hang:
            await asyncio.Event().wait()
        session = args.get("session")
        if self.mutate_session is not None and isinstance(session, KaspiSession):
            self.mutate_session(session)
        if self.before_return is not None:
            await self.before_return()
        if not self.outcomes[method]:
            raise AssertionError(f"клиент Kaspi не должен вызываться: {method}")
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def login(
        self, credentials: KaspiCredentials, previous: KaspiSession | None = None
    ) -> LoggedIn | CodeRequired:
        result: LoggedIn | CodeRequired = await self._run(
            "login", credentials=credentials, previous=previous
        )
        return result

    async def submit_code(self, session: KaspiSession, code: str) -> LoggedIn:
        result: LoggedIn = await self._run("submit_code", session=session, code=code)
        return result

    async def resend_code(self, session: KaspiSession) -> None:
        await self._run("resend_code", session=session)

    async def list_products(self, session: KaspiSession, merchant_uid: str) -> list[ListedProduct]:
        result: list[ListedProduct] = await self._run(
            "list_products", session=session, merchant_uid=merchant_uid
        )
        return result

    async def get_offers(self, master_sku: str, city_id: str, merchant_uid: str) -> OffersPage:
        """Пустая очередь - страница из `offers.json` (публичная карточка, без сессии)."""
        if not self.outcomes["get_offers"]:
            text = (FIXTURES_DIR / "offers.json").read_text(encoding="utf-8")
            self.outcomes["get_offers"].append(OffersPage.model_validate_json(text))
        result: OffersPage = await self._run(
            "get_offers", master_sku=master_sku, city_id=city_id, merchant_uid=merchant_uid
        )
        return result


def fake_client(deps: ApiDeps) -> FakeConnectClient:
    assert isinstance(deps.client, FakeConnectClient)
    return deps.client


def kaspi_session(
    phase: str = "authenticated", *, cookie: str = KASPI_COOKIE, pending: str | None = None
) -> KaspiSession:
    """Настоящая `KaspiSession`, загруженная из синтетического блока с cookie `cookie`."""
    blob = {
        "v": 1,
        "phase": phase,
        "pending_login": pending or ("mail@example.invalid" if phase == "awaiting_code" else None),
        "cookies": [
            {
                "name": "mc-session",
                "value": cookie,
                "domain": "mc.shop.kaspi.kz",
                "path": "/",
                "secure": True,
                "expires": None,
                "host_only": True,
            }
        ],
    }
    return KaspiSession.load(Secret(json.dumps(blob)))


def touch_session(session: KaspiSession, cookie: str = "COOKIE-NEW-9c1d") -> None:
    """Kaspi обновил cookie: сессия получает `changed = True`."""
    request = httpx.Request("GET", "https://mc.shop.kaspi.kz/")
    response = httpx.Response(
        200,
        headers=[("Set-Cookie", f"mc-session={cookie}; Domain=.kaspi.kz; Path=/; Secure")],
        request=request,
    )
    session.absorb(response)
    assert session.changed


def logged_in(*uids: str, session: KaspiSession | None = None) -> LoggedIn:
    return LoggedIn(
        session=session or kaspi_session(), merchants=[Merchant(uid=uid) for uid in uids]
    )


def code_required(session: KaspiSession | None = None) -> CodeRequired:
    return CodeRequired(session=session or kaspi_session("awaiting_code"))


# --- Postgres ---


def _url(name: str) -> str:
    port = int(os.environ.get("KASPISA_PG_PORT", "5432"))
    return f"postgresql+asyncpg://kaspisa:kaspisa@localhost:{port}/{name}"


async def _admin(sql: str) -> None:
    engine = create_async_engine(
        _url("postgres"), poolclass=NullPool, isolation_level="AUTOCOMMIT", hide_parameters=True
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def api_database_url() -> Iterator[str]:
    """База прогона с миграциями до head; в конце удаляется."""
    name = f"kaspisa_test_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        config = Config(str(ALEMBIC_INI))
        config.set_main_option("sqlalchemy.url", _url(name))
        command.upgrade(config, "head")
        yield _url(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def api_cipher() -> SecretCipher:
    return SecretCipher(KeyRing(1, {1: os.urandom(32)}))


# --- Redis ---


def redis_port() -> int:
    return int(os.environ.get("KASPISA_REDIS_PORT", "6379"))


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis(port=redis_port(), socket_connect_timeout=1, socket_timeout=2)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("Redis недоступен: выполните make up (порт - KASPISA_REDIS_PORT)")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def redis_prefix(redis_client: Redis) -> AsyncIterator[str]:
    """Уникальный префикс ключей теста; ключи удаляются в конце."""
    prefix = f"test:{uuid.uuid4().hex[:10]}"
    try:
        yield prefix
    finally:
        keys: list[Any] = [key async for key in redis_client.scan_iter(f"{prefix}:*")]
        if keys:
            await redis_client.delete(*keys)


# --- зависимости приложения ---


@pytest.fixture
async def deps(
    api_database_url: str,
    api_cipher: SecretCipher,
    redis_client: Redis,
    redis_prefix: str,
) -> AsyncIterator[ApiDeps]:
    """Настоящие база (пул соединений) и Redis; таблицы очищаются после теста."""
    database = create_database(api_database_url, cipher=api_cipher)
    notifier = Notifier(database)
    engine = create_async_engine(api_database_url, poolclass=NullPool, hide_parameters=True)
    try:
        yield ApiDeps(
            database=database,
            redis=redis_client,
            client=FakeConnectClient(),
            login_lock=LoginLock(redis_client, prefix=redis_prefix),
            sync_lock=ApiSyncLock(redis_client, prefix=redis_prefix),
            state=RedisState(redis_client, prefix=f"{redis_prefix}:api"),
            limiter=RedisIntervalLimiter(redis_client, prefix=redis_prefix, max_wait=10),
            price_read_lock=PriceReadLock(redis_client, prefix=redis_prefix),
            clock=FakeClock(),
            worker_prefix=redis_prefix,
            notifier=notifier,
        )
    finally:
        await notifier.aclose()
        await database.dispose()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM tenant"))
        await engine.dispose()


def make_api(
    deps: ApiDeps, *, ip: str = "127.0.0.1", app: Any = None, csrf: bool = True
) -> httpx.AsyncClient:
    """Клиент к приложению без сети; `ip` - адрес клиента для счётчиков попыток."""
    transport = httpx.ASGITransport(
        app=app if app is not None else create_app(deps),
        raise_app_exceptions=False,
        client=(ip, 50000),
    )
    return httpx.AsyncClient(
        transport=transport, base_url="https://test", headers=CSRF_HEADERS if csrf else {}
    )


async def confirm_password(client: httpx.AsyncClient) -> None:
    """Подтверждает пароль сессии: опасные правки границ (ADR 0024) проходят 10 минут."""
    response = await client.post("/api/auth/confirm-password", json={"password": SERVICE_PASSWORD})
    assert response.status_code == 204, response.text


async def signed_in(
    deps: ApiDeps,
    email: str = "a@example.com",
    password: str = SERVICE_PASSWORD,
    *,
    ip: str = "127.0.0.1",
) -> httpx.AsyncClient:
    """Новый пользователь в своей учётной записи и клиент с его cookie.

    Первый пользователь установки регистрируется через API. Регистрация после него закрыта
    (ADR 0027), поэтому второй и следующие создаются через слой данных: у них нет события
    `login` и cookie устройства, а предел регистраций с IP не тратится.
    """
    client = make_api(deps, ip=ip)
    async with deps.database.system() as system:
        first = not await system.has_users()
    if first:
        response = await client.post(
            "/api/auth/register", json={"email": email, "password": password}
        )
        assert response.status_code == 201, response.text
        return client
    async with deps.database.system() as system:
        tenant = await system.create_tenant(name=email)
        await system.commit()
    async with deps.database.tenant(TenantId(tenant.id)) as scope:
        user = await scope.users.add(email=email, password_hash=hash_password(password))
        # Один пользователь в своём арендаторе - его администратор (ADR 0039).
        user.is_admin = True
        await scope.commit()
    token = await deps.state.create_session(user.id, TenantId(tenant.id))
    client.cookies.set(SESSION_COOKIE, token)
    return client


async def signed_in_member(
    deps: ApiDeps,
    admin_email: str,
    email: str = "staff@example.com",
    password: str = SERVICE_PASSWORD,
    *,
    ip: str = "127.0.0.1",
) -> tuple[httpx.AsyncClient, uuid.UUID]:
    """Обычный (не администратор) пользователь в арендаторе `admin_email` и его клиент."""
    client = make_api(deps, ip=ip)
    async with deps.database.system() as system:
        owner = await system.find_user_by_email(admin_email)
    assert owner is not None
    tenant = TenantId(owner.tenant_id)
    async with deps.database.tenant(tenant) as scope:
        user = await scope.users.add(email=email, password_hash=hash_password(password))
        await scope.commit()
    token = await deps.state.create_session(user.id, tenant)
    client.cookies.set(SESSION_COOKIE, token)
    return client, user.id


# --- таблица маршрутов ---


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    body: dict[str, Any] | None = None

    def url(self, shop_id: uuid.UUID | str | None = None, product_id: int = 1) -> str:
        """Путь с подставленными `shop_id` и `product_id`."""
        return self.path.replace("{shop_id}", str(shop_id)).replace("{product_id}", str(product_id))


# Образцы допустимых тел: без тела FastAPI отвечает 422 до обработчика.
ROUTES: list[RouteSpec] = [
    RouteSpec("POST", "/api/auth/register", {"email": "r@example.com", "password": "x" * 12}),
    RouteSpec("POST", "/api/auth/login", {"email": "r@example.com", "password": "x" * 12}),
    RouteSpec("POST", "/api/auth/logout"),
    RouteSpec("POST", "/api/auth/logout-all"),
    RouteSpec("POST", "/api/auth/confirm-password", {"password": SERVICE_PASSWORD}),
    RouteSpec("GET", "/api/events"),
    RouteSpec("GET", "/api/auth/me"),
    RouteSpec("POST", "/api/auth/password-reset", {"token": "t", "new_password": "x" * 12}),
    # Магазины (часть 2). `{shop_id}` подставляется тестом.
    RouteSpec("GET", "/api/shops"),
    RouteSpec("POST", "/api/shops", {"login": KASPI_LOGIN, "password": KASPI_PASSWORD}),
    RouteSpec("GET", "/api/shops/{shop_id}"),
    RouteSpec(
        "PUT",
        "/api/shops/{shop_id}/credentials",
        {"login": KASPI_LOGIN, "password": KASPI_PASSWORD},
    ),
    RouteSpec("PATCH", "/api/shops/{shop_id}/settings", {"default_step": 9}),
    RouteSpec("POST", "/api/shops/{shop_id}/halt/clear"),
    RouteSpec("DELETE", "/api/shops/{shop_id}"),
    # Подключение к Kaspi (часть 3).
    RouteSpec("POST", "/api/shops/{shop_id}/login"),
    RouteSpec("POST", "/api/shops/{shop_id}/login/restart"),
    RouteSpec("POST", "/api/shops/{shop_id}/code", {"code": EMAIL_CODE}),
    RouteSpec("POST", "/api/shops/{shop_id}/code/resend"),
    # Товары и журнал (часть 4).
    RouteSpec("POST", "/api/shops/{shop_id}/products/sync"),
    RouteSpec("GET", "/api/shops/{shop_id}/products"),
    RouteSpec("GET", "/api/shops/{shop_id}/products/{product_id}"),
    RouteSpec("POST", "/api/shops/{shop_id}/products/{product_id}/price-read"),
    RouteSpec("PATCH", "/api/shops/{shop_id}/products/{product_id}", {"step": 2}),
    RouteSpec(
        "PATCH",
        "/api/shops/{shop_id}/products",
        {"product_ids": [1], "changes": {"tracked": True}},
    ),
    RouteSpec("GET", "/api/shops/{shop_id}/price-changes"),
    RouteSpec("GET", "/api/shops/{shop_id}/catalog-summary"),
    RouteSpec("GET", "/api/version"),
    # Уведомления Telegram (ADR 0025).
    RouteSpec("GET", "/api/notifications/telegram"),
    RouteSpec(
        "PUT", "/api/notifications/telegram", {"token": "123456789:" + "A" * 35, "chat_id": "42"}
    ),
    RouteSpec("DELETE", "/api/notifications/telegram"),
    RouteSpec("POST", "/api/notifications/telegram/test"),
    # Пользователи (ADR 0039). `{user_id}` подставляется тестом.
    RouteSpec("GET", "/api/users"),
    RouteSpec("POST", "/api/users", {"email": "staff@example.com"}),
    RouteSpec("DELETE", "/api/users/{user_id}"),
]


# --- начальные данные: репозитории (доступы шифруются) и сырой SQL (любое состояние) ---


@pytest.fixture
async def raw_engine(api_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(api_database_url, poolclass=NullPool, hide_parameters=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def sql_scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.begin() as conn:
        return (await conn.execute(text(statement), params)).scalar_one_or_none()


async def sql_exec(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params)


async def tenant_of(engine: AsyncEngine, email: str) -> uuid.UUID:
    found = await sql_scalar(engine, "SELECT tenant_id FROM app_user WHERE email = :e", e=email)
    assert isinstance(found, uuid.UUID)
    return found


async def seed_shop(
    deps: ApiDeps,
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    login: str = KASPI_LOGIN,
    password: str = KASPI_PASSWORD,
    state: str = "not_connected",
    merchant_uid: str | None = None,
    version: int = 0,
    error: str | None = None,
    halt_reason: str | None = None,
    halted_at: datetime | None = None,
    login_streak: int = 0,
    write_streak: int = 0,
    claimed_by: str | None = None,
    claimed_until: datetime | None = None,
    changed_at: datetime | None = None,
    **settings: Any,
) -> uuid.UUID:
    """Магазин с заданным состоянием; `settings` - столбцы настроек (`city_id` и др.)."""
    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        shop = await scope.shops.add(login=Secret(login), password=Secret(password))
        await scope.commit()
    values: dict[str, Any] = {
        "state": state,
        "uid": merchant_uid,
        "version": version,
        "error": error,
        "halt": halt_reason,
        "halted": halted_at or (datetime.now(UTC) if halt_reason else None),
        "ls": login_streak,
        "ws": write_streak,
        "cb": claimed_by,
        "cu": claimed_until,
        "sess": b"x" if state == "connected" else None,
        "id": shop.id,
    }
    await sql_exec(
        engine,
        "UPDATE shop SET connection_state = :state, merchant_uid = :uid, "
        "connection_version = :version, connection_error = :error, halt_reason = :halt, "
        "halted_at = :halted, login_error_streak = :ls, write_error_streak = :ws, "
        "claimed_by = :cb, claimed_until = :cu, session_enc = :sess WHERE id = :id",
        **values,
    )
    if changed_at is not None:
        await sql_exec(
            engine,
            "UPDATE shop SET connection_changed_at = :at WHERE id = :id",
            at=changed_at,
            id=shop.id,
        )
    for column, value in settings.items():
        assert column in {"city_id", "bot_mode", "default_step", "strategy"}
        await sql_exec(engine, f"UPDATE shop SET {column} = :v WHERE id = :id", v=value, id=shop.id)
    return shop.id


async def seed_product(
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    *,
    sku: str | None = None,
    title: str = "Товар",
    tracked: bool = False,
    min_price: int | None = None,
    max_price: int | None = None,
    **extra: Any,
) -> int:
    """Товар; `extra` - столбцы наблюдения (`refusal_reason`, `current_price` и др.)."""
    found = await sql_scalar(
        engine,
        "INSERT INTO product (tenant_id, shop_id, sku, master_sku, title, step, tracked, "
        "min_price, max_price) VALUES (:t, :s, :sku, 'm', :title, 1, :tr, :mn, :mx) RETURNING id",
        t=tenant_id,
        s=shop_id,
        sku=sku or f"sku-{uuid.uuid4().hex[:8]}",
        title=title,
        tr=tracked,
        mn=min_price,
        mx=max_price,
    )
    if "skip_reason" in extra or "skipped_at" in extra:  # CHECK требует обе колонки вместе
        await sql_exec(
            engine,
            "UPDATE product SET skip_reason = :r, skipped_at = :t WHERE id = :id",
            r=extra.pop("skip_reason", None),
            t=extra.pop("skipped_at", None),
            id=int(found),
        )
    decision = {key: extra.pop(key) for key in tuple(extra) if key.startswith("decision_")}
    if decision:  # CHECK связывает три поля решения с причиной
        assert set(decision) <= {"decision_reason", "decision_target_price", "decision_write"}
        await sql_exec(
            engine,
            "UPDATE product SET decision_reason = :r, decision_target_price = :p, "
            "decision_write = :w WHERE id = :id",
            r=decision.get("decision_reason"),
            p=decision.get("decision_target_price"),
            w=decision.get("decision_write"),
            id=int(found),
        )
    if "last_checked_at" in extra:
        await sql_exec(
            engine,
            "UPDATE product SET last_checked_at = :t WHERE id = :id",
            t=extra.pop("last_checked_at"),
            id=int(found),
        )
    for column, value in extra.items():
        assert column in {
            "refusal_reason",
            "refusal_since",
            "refusal_fingerprint",
            "retry_at",
            "current_price",
            "position",
            "position_over",
            "competitors_count",
            "first_place_price",
            "master_sku",
            "step",
        }
        await sql_exec(
            engine, f"UPDATE product SET {column} = :v WHERE id = :id", v=value, id=int(found)
        )
    return int(found)


async def seed_change(
    engine: AsyncEngine, tenant_id: uuid.UUID, shop_id: uuid.UUID, product_id: int
) -> int:
    found = await sql_scalar(
        engine,
        "INSERT INTO price_change (tenant_id, shop_id, product_id, old_price, new_price, "
        "reason, result) VALUES (:t, :s, :p, 100, 90, 'undercut_first', 'unconfirmed') "
        "RETURNING id",
        t=tenant_id,
        s=shop_id,
        p=product_id,
    )
    return int(found)


async def seed_price_write(
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    product_id: int,
    *,
    status: str,
    sent_at: datetime,
    prices: dict[str, int] | None = None,
) -> None:
    """Строка `price_write`; `next_verify_at` задан, как у открытой записи обработчика."""
    change_id = await seed_change(engine, tenant_id, shop_id, product_id)
    await sql_exec(
        engine,
        "INSERT INTO price_write (product_id, tenant_id, shop_id, sku, snapshot, "
        "expected_prices, sent_at, status, next_verify_at, price_change_id) "
        "VALUES (:p, :t, :s, 'sku', CAST(:snap AS jsonb), CAST(:exp AS jsonb), :sent, "
        ":status, :nv, :c)",
        p=product_id,
        t=tenant_id,
        s=shop_id,
        snap="{}",
        exp=json.dumps(prices or {"750000000": 90}),
        sent=sent_at,
        status=status,
        nv=sent_at + timedelta(seconds=30),
        c=change_id,
    )


# --- товары ---


def listed(
    sku: str, master_title: str | None = None, master_sku: str | None = None
) -> ListedProduct:
    """Строка списка товаров Kaspi из снятого примера с заменёнными `sku` и названием."""
    row = copy.deepcopy(load_fixture("product-list.json")["data"][0])
    row["sku"] = sku
    row["masterSku"] = master_sku or sku
    row["masterTitle"] = master_title
    return ListedProduct.model_validate(row)


async def connected_shop(
    deps: ApiDeps, engine: AsyncEngine, tenant_id: uuid.UUID, **kwargs: Any
) -> uuid.UUID:
    """Подключённый магазин с читаемой сессией (для загрузки каталога)."""
    kwargs.setdefault("merchant_uid", "M1")
    return await seed_connect_shop(
        deps, engine, tenant_id, state="connected", version=2, session=kaspi_session(), **kwargs
    )


# --- подключение к Kaspi: начальные данные ---


async def store_session(
    deps: ApiDeps,
    tenant_id: uuid.UUID,
    shop_id: uuid.UUID,
    session: KaspiSession,
    *,
    version: int,
) -> None:
    """Сохраняет блок сессии магазина (шифруется репозиторием)."""
    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        await scope.shops.save_session(shop_id, session.dump(), expected_version=version)
        await scope.commit()


async def seed_connect_shop(
    deps: ApiDeps,
    engine: AsyncEngine,
    tenant_id: uuid.UUID,
    *,
    state: str = "not_connected",
    session: KaspiSession | None = None,
    version: int = 0,
    aged: bool = True,
    **kwargs: Any,
) -> uuid.UUID:
    """Магазин для тестов входа; `aged` - письмо отправлено давно (таймер 180 с истёк)."""
    clock = deps.clock
    assert isinstance(clock, FakeClock)
    shop_id = await seed_shop(
        deps,
        engine,
        tenant_id,
        state=state,
        version=version,
        changed_at=clock.now - timedelta(seconds=200) if aged else None,
        **kwargs,
    )
    if session is not None:
        await store_session(deps, tenant_id, shop_id, session, version=version)
    return shop_id


async def sync_clock(deps: ApiDeps, engine: AsyncEngine, *, ahead: float = 1.0) -> None:
    """Часы теста - по часам базы (`connection_changed_at` ставит база), чуть впереди."""
    clock = deps.clock
    assert isinstance(clock, FakeClock)
    now = await sql_scalar(engine, "SELECT clock_timestamp()")
    clock.now = now + timedelta(seconds=ahead)


async def read_shop(engine: AsyncEngine, shop_id: uuid.UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT * FROM shop WHERE id = :id"), {"id": shop_id})
        return dict(row.mappings().one())


async def stored_session(deps: ApiDeps, tenant_id: uuid.UUID, shop_id: uuid.UUID) -> str | None:
    """Открытый текст сохранённого блока сессии (для проверок в тестах)."""
    async with deps.database.tenant(TenantId(tenant_id)) as scope:
        blob = await scope.shops.get_session(shop_id)
    return None if blob is None else blob.reveal()
