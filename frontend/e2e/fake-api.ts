// Заглушка API для сквозного теста: перехват в браузере через page.route, состояние в памяти.
import type { Page, Route } from "@playwright/test";

import type {
  CatalogSummaryOut,
  ConnectResult,
  ErrorOut,
  PendingWrite,
  PriceChange,
  PriceChangePage,
  Product,
  ProductPage,
  Shop,
  SyncResult,
  User,
  UserAdded,
  UserAdmin,
  VersionOut,
} from "../src/api/types";
import { makePendingWrite, makePriceChange, makeProduct, makeShop } from "../src/test/fixtures";

const PRICE_MAX = 100_000_000;
const isPrice = (v: unknown): v is number =>
  typeof v === "number" && Number.isInteger(v) && v >= 1 && v <= PRICE_MAX;
const PRODUCT_KEYS = ["tracked", "auto_decrease", "auto_raise", "min_price", "max_price", "step"];
const BULK_KEYS = ["tracked", "auto_decrease", "auto_raise", "step"];

/** Проверка тела по схеме сервера: пути полей 422 — `body.<поле>`; null — тело верное. */
function invalidFields(body: Record<string, unknown>, allowed: string[], nullable: string[]) {
  const fields: string[] = [];
  const path = (key: string) => `body.${key}`;
  if (Object.keys(body).length === 0) return ["body"];
  for (const [key, value] of Object.entries(body)) {
    if (!allowed.includes(key)) fields.push(path(key));
    else if (["tracked", "auto_decrease", "auto_raise"].includes(key)) {
      if (typeof value !== "boolean") fields.push(path(key));
    } else if (value === null ? !nullable.includes(key) : !isPrice(value)) fields.push(path(key));
  }
  return fields.length > 0 ? fields : null;
}

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export class FakeApi {
  user: User | null = null;
  /** Учётная запись, заведённая заранее: войти в неё можно через POST /auth/login. */
  account: { email: string; password: string } | null = null;
  /** Все пользователи установки (GET /api/users). */
  users: UserAdmin[] = [];
  /** Окно повторного пароля открыто (сбрасывается после одного изменения). */
  passwordConfirmed = false;
  shops: Shop[] = [];
  products: Product[] = [];
  priceChanges: PriceChange[] = [];
  /** Открытые записи цены по id товара; `outside_bounds` считается по формуле сервера. */
  pendingWrites = new Map<number, PendingWrite>();
  /** Если задан, загрузка каталога ждёт его разрешения (тест держит ответ). */
  syncGate: Promise<void> | null = null;
  /** Если задан, «Узнать цены» ждёт его разрешения. */
  priceReadGate: Promise<void> | null = null;
  /** Время последнего чтения цен по id товара (мс). */
  priceReadAt = new Map<number, number>();
  csrfMissing = 0;
  unhandled = 0;
  external = 0;

  /** Регистрирует маршруты; общий маршрут первым, потому что Playwright проверяет последний первым. */
  async install(page: Page, origin: string) {
    await page.route("**/*", (route) => {
      if (new URL(route.request().url()).origin === origin) return route.continue();
      this.external += 1;
      return route.abort();
    });
    await page.route("**/api/**", (route) => this.handle(route));
  }

  private error(route: Route, status: number, code: string) {
    const body: ErrorOut = { code };
    return json(route, status, body);
  }

  private handle(route: Route) {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname.replace(/^\/api/, "");

    if (method !== "GET" && request.headers()["x-requested-with"] !== "kaspisa") {
      this.csrfMissing += 1;
      return this.error(route, 403, "csrf_required");
    }

    if (method === "POST" && path === "/auth/register") {
      // Как сервер после первой учётной записи (ADR 0027): она администратор.
      if (this.account !== null || this.user !== null) {
        return this.error(route, 403, "registration_closed");
      }
      const sent = request.postDataJSON() as { email: string };
      this.user = { id: "00000000-0000-4000-8000-000000000001", email: sent.email, is_admin: true };
      return json(route, 201, this.user);
    }
    if (method === "POST" && path === "/auth/login") {
      const sent = request.postDataJSON() as { email: string; password: string };
      if (this.account?.email !== sent.email || this.account.password !== sent.password) {
        return this.error(route, 401, "invalid_credentials");
      }
      this.user = { id: "00000000-0000-4000-8000-000000000001", email: sent.email, is_admin: true };
      this.users = [
        {
          ...this.user,
          created_at: "2026-11-01T10:00:00Z",
        },
      ];
      return json(route, 200, this.user);
    }
    if (method === "POST" && path === "/auth/confirm-password") {
      const sent = request.postDataJSON() as { password: string };
      if (this.account?.password !== sent.password) return this.error(route, 403, "password_invalid");
      this.passwordConfirmed = true;
      return route.fulfill({ status: 204 });
    }
    if (path === "/users" || path.startsWith("/users/")) return this.userRoute(route, method, path);
    if (method === "GET" && path === "/auth/me") {
      return this.user ? json(route, 200, this.user) : this.error(route, 401, "unauthenticated");
    }
    if (method === "GET" && path === "/version") {
      return json(route, 200, {
        update_available: false,
        latest: null,
        current: "0.1.0",
        release_url: null,
      } satisfies VersionOut);
    }
    if (method === "POST" && path === "/auth/logout") {
      this.user = null;
      return route.fulfill({ status: 204 });
    }

    if (method === "GET" && path === "/events") return json(route, 200, []);
    if (method === "GET" && path === "/notifications/telegram") {
      return json(route, 200, { token_set: false, chat_id: null });
    }

    const shop = this.shopRoute(route, method, path);
    if (shop) return shop;

    this.unhandled += 1;
    return this.error(route, 500, "internal_error");
  }

  /** Пользователи установки: только администратор, изменения — после повторного пароля (одно действие). */
  private userRoute(route: Route, method: string, path: string) {
    if (!this.user?.is_admin) return this.error(route, 403, "admin_required");
    if (method === "GET" && path === "/users") return json(route, 200, this.users);
    if (!this.passwordConfirmed) return this.error(route, 403, "password_required");
    if (method === "POST" && path === "/users") {
      const sent = route.request().postDataJSON() as { email: string };
      if (this.users.some((u) => u.email === sent.email)) return this.error(route, 409, "email_taken");
      this.passwordConfirmed = false;
      const user: UserAdmin = {
        id: `00000000-0000-4000-8000-0000000002${String(this.users.length).padStart(2, "0")}`,
        email: sent.email,
        is_admin: false,
        created_at: "2026-11-27T10:00:00Z",
      };
      this.users.push(user);
      const body: UserAdded = { user, reset_token: "fake-reset-token", expires_in_seconds: 3600 };
      return json(route, 201, body);
    }
    const one = /^\/users\/([^/]+)$/.exec(path);
    if (method === "DELETE" && one) {
      const target = this.users.find((u) => u.id === one[1]);
      if (!target) return this.error(route, 404, "not_found");
      if (target.id === this.user.id) return this.error(route, 409, "cannot_delete_self");
      this.passwordConfirmed = false;
      this.users = this.users.filter((u) => u !== target);
      return route.fulfill({ status: 204 });
    }
    this.unhandled += 1;
    return this.error(route, 500, "internal_error");
  }

  /** Магазины и подключение; настоящий Kaspi не вызывается. Код «123456» подключает магазин. */
  private shopRoute(route: Route, method: string, path: string) {
    if (path === "/shops" && method === "GET") return json(route, 200, this.shops);
    if (path === "/shops" && method === "POST") {
      if (this.shops.length > 0) return this.error(route, 409, "shop_limit_reached");
      const shop = makeShop({ id: `00000000-0000-4000-8000-0000000001${this.shops.length}`,
        city_id: "710000000",
      });
      this.shops.push(shop);
      return json(route, 201, shop);
    }
    const match = /^\/shops\/([^/]+)(\/.*)?$/.exec(path);
    const shop = match ? this.shops.find((s) => s.id === match[1]) : undefined;
    if (!match || !shop) return null;
    const sub = match[2] ?? "";
    const reply = (outcome: ConnectResult["outcome"]) => {
      const body: ConnectResult = { outcome, retry_after_seconds: null, shop };
      return json(route, 200, body);
    };

    if (sub === "" && method === "GET") return json(route, 200, shop);
    if (sub === "" && method === "DELETE") {
      this.shops = this.shops.filter((s) => s !== shop);
      return route.fulfill({ status: 204 });
    }
    if (sub === "/login" && method === "POST") {
      Object.assign(shop, { connection_state: "awaiting_code", resend_available_in_seconds: 180 });
      return reply("code_required");
    }
    if (sub === "/code" && method === "POST") {
      const sent = route.request().postDataJSON() as { code: string };
      if (sent.code !== "123456") return reply("code_invalid");
      Object.assign(shop, {
        connection_state: "connected",
        merchant_uid: "30000001",
        resend_available_in_seconds: null,
      });
      return reply("connected");
    }
    const products = this.productRoute(route, method, sub, shop);
    if (products) return products;
    if (sub === "/price-changes" && method === "GET") return this.journalRoute(route);
    if (sub === "/settings" && method === "PATCH") {
      const sent = route.request().postDataJSON() as Record<string, unknown>;
      // Сервер запрещает лишние поля (`extra="forbid"`): `below_min` убран на этапе 13.
      if ("below_min" in sent) return this.validation(route, ["body.below_min"]);
      Object.assign(shop, sent as Partial<Shop>);
      return json(route, 200, shop);
    }
    return null;
  }
  /** Журнал: новые строки сверху, фильтр по товару, страницы без `total`. */
  private journalRoute(route: Route) {
    const url = new URL(route.request().url());
    const productId = url.searchParams.get("product_id");
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const found = this.priceChanges
      .filter((c) => productId === null || c.product_id === Number(productId))
      .sort((a, b) => b.id - a.id);
    const body: PriceChangePage = { items: found.slice(offset, offset + limit), limit, offset };
    return json(route, 200, body);
  }

  /** «Узнать цены»: первый раз 200 и товар с обновлёнными цифрами, повтор в течение 60 с — 429 (как сервер). */
  private async priceReadRoute(route: Route, productId: number) {
    const product = this.products.find((p) => p.id === productId);
    if (!product) return this.error(route, 404, "not_found");
    await (this.priceReadGate ?? Promise.resolve());
    const last = this.priceReadAt.get(productId);
    if (last !== undefined && Date.now() - last < 60_000) {
      const body: ErrorOut = { code: "price_read_too_early", retry_after_seconds: 45 };
      return json(route, 429, body);
    }
    this.priceReadAt.set(productId, Date.now());
    Object.assign(product, {
      current_price: 2998,
      position: 3,
      competitors_count: 6,
      first_place_price: 2643,
      last_checked_at: "2026-11-22T10:00:00Z",
    });
    return json(route, 200, product);
  }

  /** Открывает запись цены у товара (для проверки `outside_bounds`). */
  openPendingWrite(productId: number, o: Partial<PendingWrite> = {}) {
    this.pendingWrites.set(productId, makePendingWrite(o));
  }

  private productView(product: Product): Product {
    const pending = this.pendingWrites.get(product.id) ?? null;
    if (pending === null) return { ...product, pending_write: null };
    const outside = Object.values(pending.prices).some(
      (price) =>
        (product.min_price !== null && price < product.min_price) ||
        (product.max_price !== null && price > product.max_price),
    );
    return { ...product, pending_write: { ...pending, outside_bounds: outside } };
  }

  private validation(route: Route, fields: string[]) {
    const body: ErrorOut = { code: "validation_error", fields };
    return json(route, 422, body);
  }

  /** Каталог и товары; правила сервера: частичный PATCH, 422 по полям и min_above_max, массово без границ. */
  private productRoute(route: Route, method: string, sub: string, shop: Shop) {
    if (sub === "/products/sync" && method === "POST") {
      return (this.syncGate ?? Promise.resolve()).then(() => {
        const before = this.products.length;
        if (before === 0) {
          this.products = [1, 2, 3].map((id) =>
            makeProduct({ id, shop_id: shop.id, sku: `SKU-${id}`, title: `Товар ${id}` }),
          );
        }
        shop.catalog_synced_at = "2026-11-15T10:00:00Z";
        const body: SyncResult = {
          outcome: "synced",
          added: this.products.length - before,
          updated: before,
          missing: 0,
          retry_after_seconds: null,
          shop,
        };
        return json(route, 200, body);
      });
    }
    const url = new URL(route.request().url());
    if (sub === "/catalog-summary" && method === "GET") {
      const body: CatalogSummaryOut = {
        total: this.products.length,
        tracked: this.products.filter((p) => p.tracked).length,
      };
      return json(route, 200, body);
    }
    if (sub === "/products" && method === "GET") {
      const q = url.searchParams.get("q")?.toLowerCase();
      const flag = (name: string) => {
        const v = url.searchParams.get(name);
        return v === null ? undefined : v === "true";
      };
      const tracked = flag("tracked");
      const hasBounds = flag("has_bounds");
      const hasRefusal = flag("has_refusal");
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const found = this.products
        .filter(
          (p) =>
            (q === undefined || p.title.toLowerCase().includes(q) || p.sku.toLowerCase().includes(q)) &&
            (tracked === undefined || p.tracked === tracked) &&
            (hasBounds === undefined || (p.min_price !== null && p.max_price !== null) === hasBounds) &&
            (hasRefusal === undefined || (p.refusal_reason !== null) === hasRefusal),
        )
        // Порядок сервера: отслеживаемые первыми, внутри групп по id.
        .sort((a, b) => Number(!a.tracked) - Number(!b.tracked) || a.id - b.id);
      // pending_write в списке API всегда null
      const body: ProductPage = {
        items: found.slice(offset, offset + limit).map((p) => ({ ...p, pending_write: null })),
        total: found.length,
      };
      return json(route, 200, body);
    }
    if (sub === "/products" && method === "PATCH") {
      const sent = route.request().postDataJSON() as {
        product_ids?: unknown;
        changes?: Record<string, unknown>;
      };
      const ids = sent.product_ids;
      const fields: string[] = [];
      if (!Array.isArray(ids) || ids.length === 0 || ids.length > 500) fields.push("body.product_ids");
      const bad = invalidFields(sent.changes ?? {}, BULK_KEYS, []);
      if (bad !== null) fields.push(...bad.map((f) => f.replace("body.", "body.changes.")));
      if (fields.length > 0) return this.validation(route, fields);
      const targets = (ids as number[]).map((id) => this.products.find((p) => p.id === id));
      if (targets.some((p) => p === undefined)) return this.error(route, 404, "not_found");
      for (const p of targets as Product[]) Object.assign(p, sent.changes);
      return json(route, 200, { updated: targets.length });
    }
    const read = /^\/products\/(\d+)\/price-read$/.exec(sub);
    if (read && method === "POST") return this.priceReadRoute(route, Number(read[1]));
    const one = /^\/products\/(\d+)$/.exec(sub);
    const product = one ? this.products.find((p) => p.id === Number(one[1])) : undefined;
    if (!one) return null;
    if (!product) return this.error(route, 404, "not_found");
    if (method === "GET") return json(route, 200, this.productView(product));
    if (method === "PATCH") {
      const sent = route.request().postDataJSON() as Record<string, unknown>;
      const bad = invalidFields(sent, PRODUCT_KEYS, ["min_price", "max_price"]);
      if (bad !== null) return this.validation(route, bad);
      const lo = "min_price" in sent ? (sent.min_price as number | null) : product.min_price;
      const hi = "max_price" in sent ? (sent.max_price as number | null) : product.max_price;
      if (lo !== null && hi !== null && lo > hi) return this.error(route, 422, "min_above_max");
      // Правка товара сбрасывает пропуск и решение (как сервер, ADR 0020).
      Object.assign(product, sent, {
        skip_reason: null,
        skipped_at: null,
        decision_reason: null,
        decision_target_price: null,
        decision_write: null,
      });
      const ready = product.tracked && product.min_price !== null && product.max_price !== null;
      if (ready && !this.priceChanges.some((c) => c.product_id === product.id)) {
        this.priceChanges.push(
          makePriceChange({
            id: this.priceChanges.length + 1,
            product_id: product.id,
            sku: product.sku,
            title: product.title,
            old_price: 1500,
            new_price: 1199,
            reason: "undercut_first",
            competitor_name: "Магазин А",
            competitor_price: 1200,
            target_price: 1199,
            result: "dry_run",
          }),
        );
      }
      return json(route, 200, this.productView(product));
    }
    return null;
  }
}
