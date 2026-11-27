import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fill, t } from "@/i18n";
import { formatDateTime, formatPrice } from "@/lib/format";
import { makePendingWrite, makePriceChange, makeProduct } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { ProductPage } from "./ProductPage";

const P = makeProduct();
vi.mock("next/navigation", () => ({
  useParams: () => ({ shopId: P.shop_id, productId: String(P.id) }),
}));

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

// Журнал товара грузится параллельно с товаром: ответ выбирается по адресу, а не по порядку вызовов.
const isJournal = (url: unknown) => String(url).includes("/price-changes");
const productCalls = () => fetchMock.mock.calls.filter((c) => !isJournal(c[0]));
function serve(...products: ReturnType<typeof makeProduct>[]) {
  let i = 0;
  fetchMock.mockImplementation((url: unknown) =>
    Promise.resolve(
      isJournal(url)
        ? jsonResponse(200, { items: [makePriceChange({ product_id: P.id })] })
        : jsonResponse(200, products[Math.min(i++, products.length - 1)]),
    ),
  );
}

describe("ProductPage (Т35, Т38)", () => {
  it("загружает товар по адресу и показывает название", async () => {
    serve(makeProduct());
    render(<ProductPage />);
    expect(await screen.findByRole("heading", { name: P.title })).toBeInTheDocument();
    expect(productCalls()[0][0]).toBe(`/api/shops/${P.shop_id}/products/${P.id}`);
  });

  it("tracked без нижней границы — подсказка «Задайте обе границы»", async () => {
    serve(makeProduct({ tracked: true, min_price: null, max_price: 2000 }));
    render(<ProductPage />);
    expect(await screen.findByText(t.ui.boundsHint)).toBeInTheDocument();
  });

  it("tracked false — без данных «—», с данными значения, как в таблице (Т84)", async () => {
    serve(makeProduct({ tracked: false }));
    const empty = render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(empty.container.querySelector("[data-field='current_price']")).toHaveTextContent("—");
    expect(empty.container.querySelector("[data-field='position']")).toHaveTextContent("—");
    empty.unmount();

    serve(makeProduct({ tracked: false, current_price: 2998, position: 3 }));
    const { container } = render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(container.querySelector("[data-field='current_price']")?.textContent).toBe(formatPrice(2998));
    expect(container.querySelector("[data-field='position']")?.textContent).toBe("3");
  });

  it("позиция «20+», если своего магазина нет среди прочитанных (ADR 0016)", async () => {
    serve(makeProduct({ tracked: true, position: null, position_over: 20 }));
    const { container } = render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    const position = container.querySelector("[data-field='position']");
    expect(position?.textContent).toBe("20+");
  });

  it("tracked false и position_over — «20+»", async () => {
    serve(makeProduct({ tracked: false, position: null, position_over: 20 }));
    const { container } = render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(container.querySelector("[data-field='position']")?.textContent).toBe("20+");
  });

  it("после сохранения границ outside_bounds показывается без перезагрузки", async () => {
    serve(makeProduct({ tracked: true, min_price: 1, max_price: 2000 }),
          makeProduct({
            tracked: true,
            min_price: 1,
            max_price: 1000,
            pending_write: makePendingWrite({ prices: { "750000000": 1199 }, outside_bounds: true }),
          }),
);
    render(<ProductPage />);
    const max = await screen.findByLabelText(t.ui.maxPriceLabel);
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.change(max, { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: t.ui.save }));
    expect(await screen.findByText(t.ui.pendingOutsideBounds)).toBeInTheDocument();
    expect(productCalls()).toHaveLength(2);
  });

  it("причина пропуска и время проверки в строке «Проверка» (Т29)", async () => {
    serve(
      makeProduct({
        tracked: true,
        min_price: 1000,
        max_price: 2000,
        skip_reason: "own_price_unknown",
        skipped_at: "2026-11-15T10:00:00Z",
      }),
    );
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(screen.getByText(t.ui.colCheck)).toBeInTheDocument();
    const value = screen.getByText(new RegExp(t.skipReason.own_price_unknown));
    expect(value).toHaveTextContent(
      fill(t.ui.lastCheck, { time: formatDateTime("2026-11-15T10:00:00Z") }),
    );
  });

  it("без решения и пропуска — «ожидает проверки» без времени (Т59)", async () => {
    serve(makeProduct({ tracked: true, min_price: 1000, max_price: 2000 }));
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(screen.getByText(t.ui.colCheck)).toBeInTheDocument();
    expect(screen.getByText(t.ui.awaitingCheck)).toBeInTheDocument();
    expect(screen.queryByText(/последняя проверка/)).toBeNull();
  });

  it("не отслеживается — строки «Проверка» нет", async () => {
    serve(makeProduct({ decision_reason: "out_of_bounds" }));
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(screen.queryByText(t.ui.colCheck)).toBeNull();
  });

  it.each([
    ["out_of_bounds", t.reason.out_of_bounds],
    ["own_offer_missing", t.reason.own_offer_missing],
  ])("решение %s: причина и время проверки (Т59)", async (code, text) => {
    const at = "2026-11-15T10:00:00Z";
    serve(
      makeProduct({
        tracked: true,
        min_price: 600,
        max_price: 800,
        decision_reason: code,
        last_checked_at: at,
      }),
    );
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    const value = screen.getByText(new RegExp(text));
    expect(value).toHaveTextContent(fill(t.ui.decisionNoChange, { reason: text }));
    expect(value).toHaveTextContent(fill(t.ui.lastCheck, { time: formatDateTime(at) }));
  });

  it("решение «менять» показывает итог записи (Т59)", async () => {
    serve(
      makeProduct({
        tracked: true,
        min_price: 600,
        max_price: 800,
        decision_reason: "undercut_first",
        decision_target_price: 699,
        decision_write: "dry_run",
      }),
    );
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(screen.getByText(new RegExp(t.write.dry_run))).toBeInTheDocument();
  });

  it("итог not_live показывается текстом словаря (Т34)", async () => {
    serve(
      makeProduct({
        tracked: true,
        min_price: 600,
        max_price: 800,
        decision_reason: "undercut_first",
        decision_target_price: 699,
        decision_write: "not_live",
      }),
    );
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(screen.getByText(new RegExp(t.write.not_live))).toBeInTheDocument();
  });

  it("без границ — строка «Проверка» с noBounds (Т29)", async () => {
    serve(makeProduct({ tracked: true }));
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    expect(screen.getByText(t.ui.colCheck)).toBeInTheDocument();
    expect(screen.getAllByText(t.ui.noBounds).length).toBeGreaterThan(0);
  });

  it("без границ с bounds_missing — текст решения и время проверки (Т34)", async () => {
    const at = "2026-11-15T10:00:00Z";
    serve(makeProduct({ tracked: true, decision_reason: "bounds_missing", last_checked_at: at }));
    render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    const value = screen.getByText(/Цена не меняется: границы цены не заданы/);
    expect(value).toHaveTextContent(fill(t.ui.lastCheck, { time: formatDateTime(at) }));
  });

  it("раздел «Цены в карточке Kaspi»: кнопка обновляет цифры карточки (Т83)", async () => {
    serve(makeProduct());
    const { container } = render(<ProductPage />);
    expect(await screen.findByRole("heading", { name: t.ui.priceReadTitle })).toBeInTheDocument();
    const field = (name: string) => container.querySelector(`[data-field='${name}']`);
    expect(field("current_price")).toHaveTextContent("—");

    const fresh = makeProduct({
      current_price: 2998,
      position: 3,
      competitors_count: 6,
      first_place_price: 2643,
    });
    fetchMock.mockImplementation((url: unknown, init?: RequestInit) =>
      Promise.resolve(
        isJournal(url)
          ? jsonResponse(200, { items: [] })
          : init?.method === "POST"
            ? jsonResponse(200, fresh)
            : jsonResponse(200, makeProduct()),
      ),
    );
    const before = productCalls().length;
    fireEvent.click(screen.getByRole("button", { name: t.ui.priceRead }));
    await waitFor(() => expect(field("current_price")?.textContent).toBe(formatPrice(2998)));
    expect(field("position")?.textContent).toBe("3");
    expect(field("competitors_count")?.textContent).toBe("6");
    expect(field("first_place_price")?.textContent).toBe(formatPrice(2643));
    expect(productCalls().filter((c) => (c[1] as RequestInit).method === "GET")).toHaveLength(before);
  });

  it("404 — «Не найдено»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { code: "not_found" }));
    render(<ProductPage />);
    expect(await screen.findByText(t.errors.not_found)).toBeInTheDocument();
  });

  it("журнал товара: запрос с product_id, без ссылки на карточку (Т41)", async () => {
    serve(makeProduct());
    render(<ProductPage />);
    expect(await screen.findByText(t.reason.undercut_first)).toBeInTheDocument();
    const journal = fetchMock.mock.calls.find((c) => isJournal(c[0]));
    expect(String(journal?.[0])).toContain(`product_id=${P.id}`);
    expect(screen.queryByRole("link", { name: P.title })).toBeNull();
  });
});
