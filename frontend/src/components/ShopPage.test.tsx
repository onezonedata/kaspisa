import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { ShopPage } from "./ShopPage";
import { ShellShopProvider } from "./shell-shop";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useParams: () => ({ shopId: "11111111-1111-4111-8111-111111111111" }),
}));

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const ID = "11111111-1111-4111-8111-111111111111";
const halted = () =>
  makeShop({
    connection_state: "connected",
    merchant_uid: "30000001",
    halt_reason: "write_errors",
    halted_at: "2026-11-15T09:00:00Z",
  });

describe("ShopPage: остановка бота (Т24)", () => {
  it("«Подключён» и «Бот остановлен» видны одновременно; остановку можно снять", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, halted()))
      .mockResolvedValueOnce(jsonResponse(200, { ...halted(), halt_reason: null, halted_at: null }));
    render(<ShopPage />);
    expect(await screen.findByText(t.connectionState.connected)).toBeInTheDocument();
    expect(
      screen.getByText(`${t.ui.haltedPrefix}: ${t.haltReason.write_errors}`),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: t.ui.clearHalt }));
    await vi.waitFor(() => expect(screen.queryByText(new RegExp(t.ui.haltedPrefix))).toBeNull());
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${ID}/halt/clear`);
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("POST");
    expect(screen.queryByText(new RegExp(t.ui.haltedPrefix))).toBeNull();
    expect(screen.getByText(t.connectionState.connected)).toBeInTheDocument();
  });

  it("без halt_reason баннера нет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ connection_state: "connected" })));
    render(<ShopPage />);
    await screen.findByText(t.connectionState.connected);
    expect(screen.queryByText(new RegExp(t.ui.haltedPrefix))).toBeNull();
  });

  it("при login_errors есть подсказка, что повторное подключение остановку не снимает", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ halt_reason: "login_errors" })));
    render(<ShopPage />);
    expect(await screen.findByText(t.ui.haltLoginHint)).toBeInTheDocument();
  });
});

describe("ShopPage: запрет записи на сервере (Т29)", () => {
  it("server_dry_run = true — баннер о запрете, баннера об обработчике нет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ bot_mode: "live", server_dry_run: true })));
    render(<ShopPage />);
    expect(await screen.findByText(t.ui.bannerServerDryRun)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.bannerWorkerSilent)).toBeNull();
  });

  it("server_dry_run = null — баннер «Обработчик не отвечает»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ server_dry_run: null })));
    render(<ShopPage />);
    expect(await screen.findByText(t.ui.bannerWorkerSilent)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.bannerServerDryRun)).toBeNull();
  });

  it("server_dry_run = false — баннеров нет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ bot_mode: "live", server_dry_run: false })));
    render(<ShopPage />);
    await screen.findByText(t.connectionState.not_connected);
    expect(screen.queryByText(t.ui.bannerServerDryRun)).toBeNull();
    expect(screen.queryByText(t.ui.bannerWorkerSilent)).toBeNull();
  });
});

describe("ShopPage: «Обновить» (Т47)", () => {
  it("перечитывает магазин и показывает новый halt_reason", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, makeShop()))
      .mockResolvedValueOnce(jsonResponse(200, makeShop({ halt_reason: "store_anomalies" })));
    render(<ShopPage />);
    await screen.findByText(t.connectionState.not_connected);
    fireEvent.click(screen.getByRole("button", { name: t.ui.refresh }));
    expect(
      await screen.findByText(`${t.ui.haltedPrefix}: ${t.haltReason.store_anomalies}`),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/shops/${ID}`);
  });

  it("404 при загрузке — нейтральный текст", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { code: "not_found" }));
    render(<ShopPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.not_found);
  });
});

describe("ShopPage: сводка каталога (П1)", () => {
  it("у подключённого магазина с каталогом видны число товаров и отслеживаемые; «Обновить» перечитывает сводку", async () => {
    const shop = makeShop({
      connection_state: "connected",
      catalog_synced_at: "2026-11-15T10:00:00Z",
    });
    fetchMock.mockImplementation((url: string) =>
      Promise.resolve(
        url.endsWith("/catalog-summary")
          ? jsonResponse(200, { total: 272, tracked: 1 })
          : jsonResponse(200, shop),
      ),
    );
    render(<ShopPage />);
    expect(await screen.findByText(/Товаров в каталоге: 272/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Отслеживается: 1" })).toHaveAttribute(
      "href",
      `/shops/${ID}/products?tracked=true`,
    );
    const summaryCalls = () =>
      fetchMock.mock.calls.filter((c) => String(c[0]).endsWith("/catalog-summary")).length;
    expect(summaryCalls()).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: t.ui.refresh }));
    await vi.waitFor(() => expect(summaryCalls()).toBe(2));
  });
});

describe("ShopPage: каталог загружается", () => {
  it("статус «Каталог загружается» один, кнопка загрузки отключена", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, makeShop({ connection_state: "connected", catalog_loading: true })),
    );
    render(<ShopPage />);
    expect(await screen.findByRole("button", { name: t.ui.syncCatalog })).toBeDisabled();
    expect(screen.getAllByText(t.ui.catalogLoading)).toHaveLength(1);
    expect(
      screen.getAllByRole("status").filter((n) => n.textContent === t.ui.catalogLoading),
    ).toHaveLength(1);
  });
});

describe("ShopPage: каталог", () => {
  it("у подключённого магазина есть кнопка загрузки каталога, у неподключённого нет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ connection_state: "connected" })));
    const { unmount } = render(<ShopPage />);
    expect(await screen.findByRole("button", { name: t.ui.syncCatalog })).toBeInTheDocument();
    unmount();
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop()));
    render(<ShopPage />);
    await screen.findByText(t.connectionState.not_connected);
    expect(screen.queryByRole("button", { name: t.ui.syncCatalog })).toBeNull();
  });
});

describe("ShopPage: каркас (Т19)", () => {
  const OTHER = "22222222-2222-4222-8222-222222222222";
  const productsHref = `/shops/${ID}/products`;
  const journalHref = `/shops/${ID}/journal`;

  it("без магазина в меню ссылки на товары и журнал есть", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ connection_state: "connected" })));
    render(
      <ShellShopProvider value={{ shop: null, update: vi.fn() }}>
        <ShopPage />
      </ShellShopProvider>,
    );
    await screen.findByText(t.connectionState.connected);
    expect(screen.getByRole("link", { name: t.ui.toProducts })).toHaveAttribute("href", productsHref);
    expect(screen.getByRole("link", { name: t.ui.toJournal })).toHaveAttribute("href", journalHref);
  });

  it("при другом магазине в меню ссылки на товары и журнал есть", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ connection_state: "connected" })));
    render(
      <ShellShopProvider value={{ shop: makeShop({ id: OTHER }), update: vi.fn() }}>
        <ShopPage />
      </ShellShopProvider>,
    );
    await screen.findByText(t.connectionState.connected);
    expect(screen.getByRole("link", { name: t.ui.toProducts })).toHaveAttribute("href", productsHref);
    expect(screen.getByRole("link", { name: t.ui.toJournal })).toHaveAttribute("href", journalHref);
  });

  it("если магазин меню этот же, ссылок нет — их заменяет меню", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeShop({ connection_state: "connected" })));
    render(
      <ShellShopProvider value={{ shop: makeShop({ id: ID }), update: vi.fn() }}>
        <ShopPage />
      </ShellShopProvider>,
    );
    await screen.findByText(t.connectionState.connected);
    expect(screen.queryByRole("link", { name: t.ui.toProducts })).toBeNull();
    expect(screen.queryByRole("link", { name: t.ui.toJournal })).toBeNull();
    expect(screen.queryByRole("link", { name: t.ui.productsLink })).toBeNull();
    expect(screen.queryByRole("link", { name: t.ui.navPriceJournal })).toBeNull();
  });

  it("сообщает каркасу загруженный магазин и магазин после сохранения режима", async () => {
    const update = vi.fn();
    const loaded = makeShop({ city_id: "750000000", bot_mode: "off" });
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, loaded))
      .mockResolvedValueOnce(jsonResponse(200, { ...loaded, bot_mode: "dry_run" }));
    render(
      <ShellShopProvider value={{ shop: null, update }}>
        <ShopPage />
      </ShellShopProvider>,
    );
    await screen.findByLabelText(t.botMode.dry_run);
    expect(update).toHaveBeenCalledWith(loaded);
    fireEvent.click(screen.getByLabelText(t.botMode.dry_run));
    fireEvent.click(screen.getByRole("button", { name: t.ui.save }));
    await vi.waitFor(() =>
      expect(update).toHaveBeenLastCalledWith(expect.objectContaining({ bot_mode: "dry_run" })),
    );
  });
});
