import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import { ApiError, getCatalogSummary, register, request, setUnauthenticatedHandler } from "./client";
import { emptyResponse, htmlResponse, jsonResponse, stubFetch } from "@/test/http";

let fetchMock: ReturnType<typeof stubFetch>;

beforeEach(() => {
  fetchMock = stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request: заголовки и тело (Т1)", () => {
  it.each(["POST", "PUT", "PATCH", "DELETE"] as const)(
    "%s отправляет X-Requested-With, same-origin и JSON-тело",
    async (method) => {
      fetchMock.mockResolvedValue(jsonResponse(200, {}));
      await request(method, "/api/x", { a: 1 });
      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(init.method).toBe(method);
      expect(headers.get("X-Requested-With")).toBe("kaspisa");
      expect(headers.get("Content-Type")).toBe("application/json");
      expect(init.credentials).toBe("same-origin");
      expect(init.body).toBe(JSON.stringify({ a: 1 }));
    },
  );

  it("GET без тела и без X-Requested-With", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await request("GET", "/api/x");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(init.body).toBeUndefined();
    expect(headers.get("Content-Type")).toBeNull();
    expect(headers.get("X-Requested-With")).toBeNull();
    expect(headers.get("Accept")).toBe("application/json");
  });
});

describe("request: адрес (Т2)", () => {
  it("адрес относительный, пустые и неопределённые параметры не попадают", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await request("GET", "/api/shops/1/products", undefined, {
      q: "",
      tracked: true,
      has_bounds: undefined,
      limit: 50,
      offset: 0,
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/shops/1/products?tracked=true&limit=50&offset=0");
    expect(init).not.toHaveProperty("signal");
  });

  it("без параметров адрес остаётся путём", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, {}));
    await request("GET", "/api/auth/me");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/me");
  });

  it("путь не из /api/ отвергается без запроса", async () => {
    await expect(request("GET", "/other")).rejects.toThrow();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("request: ошибки (Т3, Т4)", () => {
  it("422 несёт code и fields", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(422, { code: "validation_error", fields: ["body.password"] }),
    );
    const error = await request("POST", "/api/x", {}).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      code: "validation_error",
      fields: ["body.password"],
      retryAfterSeconds: null,
    });
  });

  it("429 несёт retryAfterSeconds", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(429, { code: "too_many_attempts", retry_after_seconds: 60 }),
    );
    const error = await request("POST", "/api/x", {}).catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 429, retryAfterSeconds: 60, fields: null });
  });

  it("HTML от прокси даёт unexpected_error", async () => {
    fetchMock.mockResolvedValue(htmlResponse(502));
    const error = await request("GET", "/api/x").catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 502, code: "unexpected_error" });
  });

  it("200 с телом не в JSON даёт ApiError unexpected_error, а не SyntaxError", async () => {
    fetchMock.mockResolvedValue(htmlResponse(200));
    const error = await request("GET", "/api/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 200, code: "unexpected_error", fields: null });
  });

  it("JSON без строкового code (например, {detail}) даёт unexpected_error", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { detail: "boom" }));
    const noDetail = await request("GET", "/api/x").catch((e: unknown) => e);
    expect(noDetail).toMatchObject({ status: 500, code: "unexpected_error", fields: null });
    fetchMock.mockResolvedValue(jsonResponse(500, { code: 42 }));
    const badType = await request("GET", "/api/x").catch((e: unknown) => e);
    expect(badType).toMatchObject({ status: 500, code: "unexpected_error" });
  });

  it("отклонённый fetch даёт network_error со статусом 0", async () => {
    fetchMock.mockRejectedValue(new TypeError("failed"));
    const error = await request("GET", "/api/x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: "network_error" });
  });

  it("204 возвращает undefined без чтения тела", async () => {
    const response = emptyResponse(204);
    const json = vi.spyOn(response, "json");
    fetchMock.mockResolvedValue(response);
    await expect(request("POST", "/api/auth/logout")).resolves.toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });
});

describe("request: повторов нет (Т5)", () => {
  it.each([
    [503, { code: "server_busy", retry_after_seconds: 1 }],
    [503, { code: "service_unavailable" }],
    [429, { code: "too_many_attempts", retry_after_seconds: 5 }],
    [500, { code: "internal_error" }],
  ])("статус %i %j: один вызов fetch", async (status, body) => {
    fetchMock.mockResolvedValue(jsonResponse(status, body));
    await expect(request("POST", "/api/x", {})).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("request: обработчик «не вошёл» (Т6)", () => {
  const handler = vi.fn();
  beforeEach(() => {
    handler.mockReset();
    setUnauthenticatedHandler(handler);
  });

  it("401 unauthenticated на /api/shops вызывает обработчик один раз", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { code: "unauthenticated" }));
    await expect(request("GET", "/api/shops")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("401 на /api/auth/* не вызывает обработчик", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "invalid_credentials" }));
    await expect(request("POST", "/api/auth/login", {})).rejects.toBeInstanceOf(ApiError);
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { code: "unauthenticated" }));
    await expect(request("GET", "/api/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});

describe("getCatalogSummary (Т26)", () => {
  it("GET на /catalog-summary магазина без тела и без служебного заголовка, отдаёт счётчики", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { total: 272, tracked: 1 }));
    const summary = await getCatalogSummary("s1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/shops/s1/catalog-summary");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(summary).toEqual({ total: 272, tracked: 1 });
  });

  it("каталог больше предела: null в обоих счётчиках не считается ошибкой", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { total: null, tracked: null }));
    await expect(getCatalogSummary("s1")).resolves.toEqual({ total: null, tracked: null });
  });

  it("404 для чужого магазина превращается в ApiError с кодом not_found", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { code: "not_found", message: "нет" }));
    await expect(getCatalogSummary("s1")).rejects.toMatchObject({
      status: 404,
      code: "not_found",
    });
  });
});

describe("типы ответов", () => {
  it("register: тело ответа 201 попадает в тип, а не сводится к never (проверяет tsc)", () => {
    type Registered = Awaited<ReturnType<typeof register>>;
    expectTypeOf<Registered>().not.toBeNever();
    expectTypeOf<Registered>().toHaveProperty("email");
  });
});
