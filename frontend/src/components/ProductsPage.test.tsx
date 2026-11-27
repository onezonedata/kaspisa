import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Shop } from "@/api/types";
import { fill, t } from "@/i18n";
import { makeProduct, makeShop } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { ProductsPage } from "./ProductsPage";
import { ShellShopProvider } from "./shell-shop";

const SHOP = makeProduct().shop_id;
const nav = vi.hoisted(() => ({ query: "" }));
vi.mock("next/navigation", () => ({
  useParams: () => ({ shopId: "11111111-1111-4111-8111-111111111111" }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));

let fetchMock: ReturnType<typeof stubFetch>;
let summary: () => Response;
const url = (call: unknown[]) => String(call[0]);
const method = (call: unknown[]) => ((call[1] as RequestInit | undefined)?.method ?? "GET");
const listCalls = () =>
  fetchMock.mock.calls.filter((c) => method(c) === "GET" && url(c).includes("/products?"));
const summaryCalls = () => fetchMock.mock.calls.filter((c) => url(c).endsWith("/catalog-summary"));

beforeEach(() => {
  nav.query = "";
  summary = () => jsonResponse(200, { total: 1, tracked: 0 });
  fetchMock = stubFetch();
  fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
    const address = String(input);
    if (address.endsWith("/catalog-summary")) return Promise.resolve(summary());
    if (init?.method === "POST") return Promise.resolve(jsonResponse(500, {}));
    return Promise.resolve(jsonResponse(200, { items: [makeProduct()], total: 1 }));
  });
});
afterEach(() => vi.unstubAllGlobals());

function renderPage(shop: Shop | null, update: (shop: Shop) => void = () => {}) {
  return render(
    <ShellShopProvider value={{ shop, update }}>
      <ProductsPage />
    </ShellShopProvider>,
  );
}

describe("ProductsPage: фильтр из адреса (Т30)", () => {
  it("?tracked=true — первый запрос с tracked=true, в фильтре «Да»", async () => {
    nav.query = "tracked=true";
    renderPage(null);
    await screen.findByText("SKU-1");
    expect(url(listCalls()[0])).toBe(`/api/shops/${SHOP}/products?tracked=true&limit=50&offset=0`);
    expect(screen.getByLabelText(t.ui.filterTracked)).toHaveValue("true");
  });

  it("без параметра — фильтр «Все», в запросе нет tracked", async () => {
    renderPage(null);
    await screen.findByText("SKU-1");
    expect(url(listCalls()[0])).not.toContain("tracked");
    expect(screen.getByLabelText(t.ui.filterTracked)).toHaveValue("");
  });
});

describe("ProductsPage: загрузка каталога над таблицей (Т34)", () => {
  const connected = makeShop({ id: SHOP, connection_state: "connected", catalog_loading: false });

  it("после загрузки магазин каркаса обновлён, список перечитан с тем же фильтром", async () => {
    nav.query = "tracked=true";
    const fresh = makeShop({ id: SHOP, connection_state: "connected", bot_mode: "live" });
    fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
      if (String(input).endsWith("/catalog-summary")) return Promise.resolve(summary());
      if (init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(200, { outcome: "synced", added: 1, updated: 0, missing: 0, shop: fresh }),
        );
      }
      return Promise.resolve(jsonResponse(200, { items: [makeProduct()], total: 1 }));
    });
    const update = vi.fn();
    renderPage(connected, update);
    await screen.findByText("SKU-1");
    expect(listCalls()).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: t.ui.syncCatalog }));

    await waitFor(() => expect(update).toHaveBeenCalledWith(fresh));
    await waitFor(() => expect(listCalls()).toHaveLength(2));
    expect(url(listCalls()[1])).toBe(url(listCalls()[0]));
    expect(await screen.findByText(fill(t.ui.syncDone, { added: 1, updated: 0, missing: 0 }))).toBeVisible();
    expect(
      fetchMock.mock.calls.filter((c) => method(c) === "POST" && url(c) === `/api/shops/${SHOP}/products/sync`),
    ).toHaveLength(1);
  });

  it.each([
    ["магазина в каркасе нет", null],
    ["магазин другой", makeShop({ id: "22222222-2222-4222-8222-222222222222", connection_state: "connected" })],
    ["магазин не подключён", makeShop({ id: SHOP, connection_state: "not_connected" })],
  ])("кнопки нет: %s", async (_name, shop) => {
    renderPage(shop);
    await screen.findByText("SKU-1");
    expect(screen.queryByRole("button", { name: t.ui.syncCatalog })).toBeNull();
  });
});

describe("ProductsPage: счётчик каталога (Т35)", () => {
  it("всего и отслеживается; имя заголовка не меняется", async () => {
    summary = () => jsonResponse(200, { total: 272, tracked: 5 });
    renderPage(null);
    expect(await screen.findByText(fill(t.ui.productsCount, { total: 272, tracked: 5 }))).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: t.ui.productsTitle })).toBeInTheDocument();
  });

  it("каталог слишком велик — только отслеживаемые", async () => {
    summary = () => jsonResponse(200, { total: null, tracked: 3 });
    renderPage(null);
    expect(await screen.findByText(fill(t.ui.trackedCount, { n: 3 }))).toBeInTheDocument();
  });

  it("нет и числа отслеживаемых — ноль", async () => {
    summary = () => jsonResponse(200, { total: null, tracked: null });
    renderPage(null);
    expect(await screen.findByText(fill(t.ui.trackedCount, { n: 0 }))).toBeInTheDocument();
  });

  it("ошибка сводки — счётчика и сообщения нет, таблица показана", async () => {
    summary = () => jsonResponse(500, {});
    renderPage(null);
    await screen.findByText("SKU-1");
    await waitFor(() => expect(summaryCalls()).toHaveLength(1));
    expect(screen.queryByText(/Отслеживается: \d|·/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("после загрузки каталога сводка запрошена снова", async () => {
    const connected = makeShop({ id: SHOP, connection_state: "connected" });
    fetchMock.mockImplementation((input: unknown, init?: RequestInit) => {
      if (String(input).endsWith("/catalog-summary")) return Promise.resolve(summary());
      if (init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(200, { outcome: "synced", added: 1, updated: 0, missing: 0, shop: connected }),
        );
      }
      return Promise.resolve(jsonResponse(200, { items: [makeProduct()], total: 1 }));
    });
    renderPage(connected);
    await screen.findByText("SKU-1");
    await waitFor(() => expect(summaryCalls()).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: t.ui.syncCatalog }));
    await waitFor(() => expect(summaryCalls()).toHaveLength(2));
  });
});
