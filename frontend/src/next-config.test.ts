import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

async function loadConfig() {
  vi.resetModules();
  return (await import("../next.config")).default;
}

beforeEach(() => {
  vi.unstubAllEnvs();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("next.config (Т10)", () => {
  it("rewrites ведёт /api на бэкенд по умолчанию", async () => {
    vi.stubEnv("KASPISA_API_ORIGIN", "");
    delete process.env.KASPISA_API_ORIGIN;
    const config = await loadConfig();
    expect(await config.rewrites!()).toEqual([
      { source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" },
    ]);
  });

  it("KASPISA_API_ORIGIN меняет адрес бэкенда", async () => {
    vi.stubEnv("KASPISA_API_ORIGIN", "http://x:9000");
    const config = await loadConfig();
    expect(await config.rewrites!()).toEqual([
      { source: "/api/:path*", destination: "http://x:9000/api/:path*" },
    ]);
  });

  it("таймаут прокси покрывает загрузку каталога", async () => {
    const config = await loadConfig();
    expect(config.experimental?.proxyTimeout).toBeGreaterThanOrEqual(250_000);
  });

  it("distDir: .next по умолчанию, .next-e2e по переменной", async () => {
    delete process.env.KASPISA_E2E_DIST_DIR;
    expect((await loadConfig()).distDir ?? ".next").toBe(".next");
    vi.stubEnv("KASPISA_E2E_DIST_DIR", ".next-e2e");
    expect((await loadConfig()).distDir).toBe(".next-e2e");
  });

  it("output: standalone для сборки образа", async () => {
    expect((await loadConfig()).output).toBe("standalone");
  });

  it("cacheComponents остаётся включённым", async () => {
    expect((await loadConfig()).cacheComponents).toBe(true);
  });
});
