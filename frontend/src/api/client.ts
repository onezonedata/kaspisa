// Клиент API: единая обёртка request и тонкие функции точек.
import type { paths } from "./schema";
import type {
  BulkChanges,
  CatalogSummaryOut,
  ConnectResult,
  ErrorOut,
  OwnerEvent,
  PriceChangePage,
  Product,
  ProductPage,
  ProductSettingsIn,
  Shop,
  ShopSettingsIn,
  SyncResult,
  TelegramSettings,
  TelegramSettingsIn,
  User,
  VersionOut,
} from "./types";

export class ApiError extends Error {
  /** 0 — сеть недоступна. */
  status: number;
  /** ErrorOut.code, либо "network_error" / "unexpected_error". */
  code: string;
  /** Пути полей, только для 422. */
  fields: string[] | null;
  retryAfterSeconds: number | null;

  constructor(
    status: number,
    code: string,
    fields: string[] | null = null,
    retryAfterSeconds: number | null = null,
  ) {
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fields = fields;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
type Query = Record<string, string | number | boolean | undefined>;

// Полная перезагрузка намеренна: сбрасывает всё состояние клиента.
// eslint-disable-next-line @next/next/no-location-assign-relative-destination
let onUnauthenticated: () => void = () => window.location.assign("/login");

/** Что делать, когда сессия закончилась (по умолчанию — переход на /login). */
export function setUnauthenticatedHandler(fn: () => void): void {
  onUnauthenticated = fn;
}

function buildUrl(path: string, query?: Query): string {
  if (!path.startsWith("/api/")) {
    throw new Error(`Путь должен начинаться с /api/: ${path}`);
  }
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return text === "" ? path : `${path}?${text}`;
}

async function toApiError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as Partial<ErrorOut> | null;
    if (body !== null && typeof body === "object" && typeof body.code === "string") {
      return new ApiError(res.status, body.code, body.fields ?? null, body.retry_after_seconds ?? null);
    }
  } catch {
    // тело не JSON (HTML от прокси, пусто)
  }
  return new ApiError(res.status, "unexpected_error");
}

/**
 * Один запрос к API. Без таймаута и без повторов: вход в Kaspi идёт до 45 с,
 * каталог до 240 с, а повтор расходует счётчики попыток входа.
 */
export async function request<T>(
  method: Method,
  path: string,
  body?: unknown,
  query?: Query,
): Promise<T> {
  const url = buildUrl(path, query);
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  if (method !== "GET") headers["X-Requested-With"] = "kaspisa";

  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new ApiError(0, "network_error");
  }
  if (res.status === 204) return undefined as T;
  if (res.ok) {
    try {
      return (await res.json()) as T;
    } catch {
      throw new ApiError(res.status, "unexpected_error");
    }
  }

  const error = await toApiError(res);
  if (res.status === 401 && error.code === "unauthenticated" && !path.startsWith("/api/auth/")) {
    onUnauthenticated();
  }
  throw error;
}

type JsonBody<R> = R extends { content: { "application/json": infer J } } ? J : never;
/** Тело успешного ответа (200 или 201) точки схемы. */
type Ok<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  responses: infer R;
}
  ? JsonBody<R[Extract<keyof R, 200 | 201>]>
  : never;

// Типы ответов привязаны к путям схемы: удалённый путь ломает tsc.
const AUTH = "/api/auth";
const shopPath = (id: string) => `/api/shops/${encodeURIComponent(id)}`;

export const register = (email: string, password: string) =>
  request<Ok<"/api/auth/register", "post"> & User>("POST", `${AUTH}/register`, { email, password });
export const login = (email: string, password: string) =>
  request<Ok<"/api/auth/login", "post"> & User>("POST", `${AUTH}/login`, { email, password });
export const logout = () => request<void>("POST", `${AUTH}/logout`);
export const logoutAll = () => request<void>("POST", `${AUTH}/logout-all`);
export const me = () => request<Ok<"/api/auth/me", "get"> & User>("GET", `${AUTH}/me`);
/** Повторный ввод пароля сервиса перед опасной правкой (ADR 0024). */
export const confirmPassword = (password: string) =>
  request<void>("POST", `${AUTH}/confirm-password`, { password });
export const resetPassword =(token: string, newPassword: string) =>
  request<void>("POST", `${AUTH}/password-reset`, { token, new_password: newPassword });

export const listEvents = () => request<OwnerEvent[]>("GET", "/api/events");

export const listShops =() => request<Ok<"/api/shops", "get"> & Shop[]>("GET", "/api/shops");
export const getShop = (id: string) => request<Shop>("GET", shopPath(id));
export const addShop = (login: string, password: string) =>
  request<Shop>("POST", "/api/shops", { login, password });
export const deleteShop = (id: string) => request<void>("DELETE", shopPath(id));
export const setCredentials = (id: string, login: string, password: string) =>
  request<Shop>("PUT", `${shopPath(id)}/credentials`, { login, password });
export const updateShopSettings = (id: string, patch: Partial<ShopSettingsIn>) =>
  request<Shop>("PATCH", `${shopPath(id)}/settings`, patch);
export const clearHalt = (id: string) => request<Shop>("POST", `${shopPath(id)}/halt/clear`);

export const startLogin = (id: string) =>
  request<ConnectResult>("POST", `${shopPath(id)}/login`);
export const restartLogin = (id: string) =>
  request<ConnectResult>("POST", `${shopPath(id)}/login/restart`);
export const submitCode = (id: string, code: string) =>
  request<ConnectResult>("POST", `${shopPath(id)}/code`, { code });
export const resendCode = (id: string) =>
  request<ConnectResult>("POST", `${shopPath(id)}/code/resend`);
export const syncCatalog = (id: string) =>
  request<SyncResult>("POST", `${shopPath(id)}/products/sync`);

const TELEGRAM = "/api/notifications/telegram";
export const getTelegram = () => request<TelegramSettings>("GET", TELEGRAM);
export const saveTelegram = (body: TelegramSettingsIn) =>
  request<TelegramSettings>("PUT", TELEGRAM, body);
export const deleteTelegram = () => request<void>("DELETE", TELEGRAM);
export const testTelegram = () => request<void>("POST", `${TELEGRAM}/test`);

export const getVersion = () => request<VersionOut>("GET", "/api/version");

export const getCatalogSummary = (id: string) =>
  request<CatalogSummaryOut>("GET", `${shopPath(id)}/catalog-summary`);

export interface ProductFilter {
  q?: string;
  tracked?: boolean;
  hasBounds?: boolean;
  hasRefusal?: boolean;
  limit: number;
  offset: number;
}

export const listProducts = (id: string, filter: ProductFilter) =>
  request<ProductPage>("GET", `${shopPath(id)}/products`, undefined, {
    q: filter.q,
    tracked: filter.tracked,
    has_bounds: filter.hasBounds,
    has_refusal: filter.hasRefusal,
    limit: filter.limit,
    offset: filter.offset,
  });
export const getProduct = (id: string, productId: number) =>
  request<Product>("GET", `${shopPath(id)}/products/${productId}`);
export const updateProduct = (id: string, productId: number, patch: Partial<ProductSettingsIn>) =>
  request<Product>("PATCH", `${shopPath(id)}/products/${productId}`, patch);
export const readPrices = (id: string, productId: number) =>
  request<Product>("POST", `${shopPath(id)}/products/${productId}/price-read`);
export const bulkUpdate = (id: string, productIds: number[], changes: Partial<BulkChanges>) =>
  request<{ updated: number }>("PATCH", `${shopPath(id)}/products`, {
    product_ids: productIds,
    changes,
  });
export const listPriceChanges = (
  id: string,
  opts: { productId?: number; limit: number; offset: number },
) =>
  request<PriceChangePage>("GET", `${shopPath(id)}/price-changes`, undefined, {
    product_id: opts.productId,
    limit: opts.limit,
    offset: opts.offset,
  });
