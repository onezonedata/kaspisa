import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Product, ProductPage } from "@/api/types";
import { fill, t } from "@/i18n";
import { formatDateTime, formatPrice } from "@/lib/format";
import { makeProduct } from "@/test/fixtures";
import { jsonResponse, sentBody, stubFetch } from "@/test/http";
import { ProductsTable } from "./ProductsTable";

const SHOP = makeProduct().shop_id;
let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

function page(items: Product[], total = items.length): ProductPage {
  return { items, total };
}
const rows = (n: number, from = 1): Product[] =>
  Array.from({ length: n }, (_, i) =>
    makeProduct({ id: from + i, sku: `SKU-${from + i}`, title: `Товар ${from + i}` }),
  );
const listUrls = () =>
  fetchMock.mock.calls
    .filter((c) => (c[1] as RequestInit).method === "GET")
    .map((c) => c[0] as string);
const lastList = () => listUrls().at(-1) as string;
const row = (sku: string) => screen.getByText(sku).closest("tr") as HTMLElement;
const cell = (sku: string, column: string) => {
  const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
  const index = headers.indexOf(column);
  return within(row(sku)).getAllByRole("cell")[index];
};

describe("ProductsTable: правило показа цен (Т29)", () => {
  const data = {
    id: 5,
    sku: "A-1",
    current_price: 12990,
    position: 2,
    competitors_count: 5,
    first_place_price: 12500,
  };

  it("tracked false без данных — в четырёх ячейках «—»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page([makeProduct({ id: 5, sku: "A-1", tracked: false })])));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("A-1");
    for (const column of [t.ui.colPrice, t.ui.colPosition, t.ui.colCompetitors, t.ui.colFirstPrice]) {
      expect(cell("A-1", column)).toHaveTextContent("—");
    }
  });

  it("tracked false с данными (прочитаны кнопкой) — значения показаны (ADR 0033)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page([makeProduct({ ...data, tracked: false })])));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("A-1");
    expect(cell("A-1", t.ui.colPrice).textContent).toBe(formatPrice(12990));
    expect(cell("A-1", t.ui.colPosition).textContent).toBe("2");
    expect(cell("A-1", t.ui.colCompetitors).textContent).toBe("5");
    expect(cell("A-1", t.ui.colFirstPrice).textContent).toBe(formatPrice(12500));
  });

  it("tracked true — значения показаны", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page([makeProduct({ ...data, tracked: true })])));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("A-1");
    expect(cell("A-1", t.ui.colPrice).textContent).toBe(formatPrice(12990));
    expect(cell("A-1", t.ui.colPosition)).toHaveTextContent("2");
    expect(cell("A-1", t.ui.colCompetitors)).toHaveTextContent("5");
    expect(cell("A-1", t.ui.colFirstPrice).textContent).toBe(formatPrice(12500));
  });

  it("своего магазина нет среди прочитанных — позиция «20+» (ADR 0016)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([makeProduct({ ...data, tracked: true, position: null, position_over: 20 })]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("A-1");
    expect(cell("A-1", t.ui.colPosition).textContent).toBe("20+");
  });

  it("tracked true и current_price null — «—»", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, page([makeProduct({ ...data, tracked: true, current_price: null })])),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("A-1");
    expect(cell("A-1", t.ui.colPrice)).toHaveTextContent("—");
  });

  it("отказ показан текстом с пометкой о повторе (Т35)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([makeProduct({ id: 5, sku: "A-1", refusal_reason: "permanent:stock_counted" })]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("A-1");
    expect(cell("A-1", t.ui.colRefusal)).toHaveTextContent(t.refusal.stock_counted);
    expect(cell("A-1", t.ui.colRefusal)).toHaveTextContent(t.refusal.permanentNote);
  });
});

describe("ProductsTable: выключатель «Отслеживать» (Т30)", () => {
  it("тело ровно {tracked:true}, строка обновляется из ответа", async () => {
    fetchMock.mockImplementation((url: string, init: RequestInit) =>
      Promise.resolve(
        init.method === "PATCH"
          ? jsonResponse(200, makeProduct({ id: 1, sku: "SKU-1", tracked: true, current_price: 777 }))
          : jsonResponse(200, page(rows(2))),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-1" })));
    await vi.waitFor(() => expect(cell("SKU-1", t.ui.colPrice).textContent).toBe(formatPrice(777)));
    const patch = fetchMock.mock.calls.find((c) => (c[1] as RequestInit).method === "PATCH");
    expect(patch?.[0]).toBe(`/api/shops/${SHOP}/products/1`);
    expect(JSON.parse((patch?.[1] as RequestInit).body as string)).toEqual({ tracked: true });
    expect(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-1" }))).toBeChecked();
  });

  it("ответ 409 — переключатель вернулся, ошибка видна", async () => {
    fetchMock.mockImplementation((url: string, init: RequestInit) =>
      Promise.resolve(
        init.method === "PATCH"
          ? jsonResponse(409, { code: "operation_not_allowed" })
          : jsonResponse(200, page(rows(1))),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    const toggle = screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-1" }));
    fireEvent.click(toggle);
    expect(await screen.findByText(t.errors.operation_not_allowed)).toBeInTheDocument();
    expect(toggle).not.toBeChecked();
    expect(toggle).toBeEnabled();
  });
});

describe("ProductsTable: подписи отслеживания (Т27)", () => {
  it("текст состояния в строке, переключатель и флажок — разные роли, заголовки столбцов", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([
          makeProduct({ id: 1, sku: "SKU-1", tracked: true }),
          makeProduct({ id: 2, sku: "SKU-2", tracked: false }),
        ]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(within(row("SKU-1")).getByText(t.ui.trackedOn)).toBeInTheDocument();
    expect(within(row("SKU-2")).getByText(t.ui.trackedOff)).toBeInTheDocument();
    expect(within(row("SKU-1")).getByRole("switch")).toBeChecked();
    expect(within(row("SKU-1")).getByRole("checkbox")).not.toBeChecked();
    const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toContain("Выбор");
    expect(headers).toContain("Отслеживать");
  });
});

describe("ProductsTable: проверка товара (Т28)", () => {
  it("отслеживаемый без границ — noBoundsShort с полным текстом в подсказке; с границами и причиной — текст словаря", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([
          makeProduct({ id: 1, sku: "SKU-1", tracked: true }),
          makeProduct({
            id: 2,
            sku: "SKU-2",
            tracked: true,
            min_price: 1000,
            max_price: 2000,
            skip_reason: "response_format",
            skipped_at: "2026-11-15T10:00:00Z",
          }),
          makeProduct({ id: 3, sku: "SKU-3", tracked: true, min_price: 1000, max_price: 2000 }),
        ]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(cell("SKU-1", t.ui.colCheck)).toHaveTextContent(t.ui.noBoundsShort);
    expect(cell("SKU-1", t.ui.colCheck)).not.toHaveTextContent(t.ui.noBounds);
    expect(within(cell("SKU-1", t.ui.colCheck)).getByTitle(t.ui.noBounds)).toBeInTheDocument();
    expect(cell("SKU-2", t.ui.colCheck)).toHaveTextContent(t.skipReason.response_format);
    expect(cell("SKU-3", t.ui.colCheck)).toHaveTextContent(t.ui.awaitingCheck);
    expect(cell("SKU-3", t.ui.colCheck)).not.toHaveTextContent(/последняя проверка/);
  });

  it("без границ с bounds_missing и цифрами — решение, время и цифры (Т33)", async () => {
    const at = "2026-11-15T10:00:00Z";
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([
          makeProduct({
            id: 1,
            sku: "SKU-1",
            tracked: true,
            decision_reason: "bounds_missing",
            last_checked_at: at,
            current_price: 2998,
            position: 3,
            competitors_count: 6,
            first_place_price: 2643,
          }),
        ]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(cell("SKU-1", t.ui.colCheck)).toHaveTextContent(t.ui.noBoundsShort);
    expect(
      within(cell("SKU-1", t.ui.colCheck)).getByTitle("Цена не меняется: границы цены не заданы"),
    ).toBeInTheDocument();
    expect(cell("SKU-1", t.ui.colCheck)).toHaveTextContent(
      fill(t.ui.lastCheck, { time: formatDateTime(at) }),
    );
    expect(cell("SKU-1", t.ui.colPrice).textContent).toBe(formatPrice(2998));
    expect(cell("SKU-1", t.ui.colPosition).textContent).toBe("3");
    expect(cell("SKU-1", t.ui.colCompetitors).textContent).toBe("6");
    expect(cell("SKU-1", t.ui.colFirstPrice).textContent).toBe(formatPrice(2643));
  });

  it("у не отслеживаемого товара есть «Узнать цены» и нажатие обновляет строку (Т82)", async () => {
    const answer = makeProduct({
      id: 1,
      sku: "SKU-1",
      tracked: false,
      current_price: 2998,
      position: 3,
      competitors_count: 6,
      first_place_price: 2643,
      last_checked_at: "2026-11-22T10:00:00Z",
    });
    fetchMock.mockImplementation((url: string, init?: RequestInit) =>
      Promise.resolve(
        init?.method === "POST" && url.endsWith("/price-read")
          ? jsonResponse(200, answer)
          : jsonResponse(200, page([makeProduct({ id: 1, sku: "SKU-1" })])),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    for (const column of [t.ui.colPrice, t.ui.colPosition, t.ui.colCompetitors, t.ui.colFirstPrice]) {
      expect(cell("SKU-1", column)).toHaveTextContent("—");
    }
    fireEvent.click(within(cell("SKU-1", t.ui.colPriceRead)).getByRole("button", { name: t.ui.priceRead }));
    await waitFor(() => expect(cell("SKU-1", t.ui.colPrice).textContent).toBe(formatPrice(2998)));
    expect(cell("SKU-1", t.ui.colPosition).textContent).toBe("3");
    expect(cell("SKU-1", t.ui.colCompetitors).textContent).toBe("6");
    expect(cell("SKU-1", t.ui.colFirstPrice).textContent).toBe(formatPrice(2643));
    expect(cell("SKU-1", t.ui.colCheck).textContent).toContain(
      fill(t.ui.lastCheck, { time: formatDateTime("2026-11-22T10:00:00Z") }),
    );
    // В «Цены Kaspi» только кнопка; список не перезагружался.
    expect(cell("SKU-1", t.ui.colPriceRead).textContent).toBe(t.ui.priceRead);
    expect(listUrls().filter((u) => u.includes("/products"))).toHaveLength(1);
  });

  it("решение «не менять» и время проверки в строке (Т58)", async () => {
    const at = "2026-11-15T10:00:00Z";
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([
          makeProduct({
            id: 1,
            sku: "SKU-1",
            tracked: true,
            min_price: 600,
            max_price: 800,
            decision_reason: "own_offer_missing",
            last_checked_at: at,
          }),
          makeProduct({
            id: 2,
            sku: "SKU-2",
            tracked: true,
            min_price: 600,
            max_price: 800,
            decision_reason: "out_of_bounds",
            last_checked_at: at,
          }),
          makeProduct({ id: 3, sku: "SKU-3" }),
        ]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    const time = fill(t.ui.lastCheck, { time: formatDateTime(at) });
    expect(cell("SKU-1", t.ui.colCheck)).toHaveTextContent(t.reason.own_offer_missing);
    expect(cell("SKU-1", t.ui.colCheck)).toHaveTextContent(time);
    expect(cell("SKU-2", t.ui.colCheck)).toHaveTextContent(t.reason.out_of_bounds);
    expect(cell("SKU-2", t.ui.colCheck)).toHaveTextContent(time);
    expect(cell("SKU-3", t.ui.colCheck)).toBeEmptyDOMElement();
  });
});

describe("ProductsTable: подсказка о порядке (Т37)", () => {
  it("после переключения видна подсказка, строка на месте; «Обновить» её снимает", async () => {
    fetchMock.mockImplementation((url: string, init: RequestInit) =>
      Promise.resolve(
        init.method === "PATCH"
          ? jsonResponse(200, makeProduct({ id: 2, sku: "SKU-2", tracked: true }))
          : jsonResponse(200, page(rows(2))),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(screen.queryByText(t.ui.orderHint)).toBeNull();
    fireEvent.click(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-2" })));
    expect(await screen.findByText(t.ui.orderHint)).toBeInTheDocument();
    const order = screen.getAllByRole("row").map((r) => r.textContent);
    expect(order[1]).toContain("SKU-1");
    expect(order[2]).toContain("SKU-2");
    fireEvent.click(screen.getByRole("button", { name: t.ui.refresh }));
    await vi.waitFor(() => expect(screen.queryByText(t.ui.orderHint)).toBeNull());
  });
});

describe("ProductsTable: начальный фильтр", () => {
  it("initialTracked=true — запрос с tracked=true, в фильтре «Да»", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page(rows(1))));
    render(<ProductsTable shopId={SHOP} initialTracked="true" />);
    await screen.findByText("SKU-1");
    expect(listUrls()[0]).toContain("tracked=true");
    expect(screen.getByLabelText(t.ui.filterTracked)).toHaveValue("true");
  });
});

describe("ProductsTable: быстрые переключения", () => {
  it("два ответа в обратном порядке — обе строки остаются включёнными", async () => {
    const resolvers = new Map<number, (r: Response) => void>();
    fetchMock.mockImplementation((url: string, init: RequestInit) => {
      if (init.method !== "PATCH") return Promise.resolve(jsonResponse(200, page(rows(2))));
      const id = Number(url.split("/").at(-1));
      return new Promise<Response>((resolve) => resolvers.set(id, resolve));
    });
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-1" })));
    fireEvent.click(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-2" })));
    await vi.waitFor(() => expect(resolvers.size).toBe(2));
    resolvers.get(2)?.(jsonResponse(200, makeProduct({ id: 2, sku: "SKU-2", tracked: true })));
    await vi.waitFor(() =>
      expect(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-2" }))).toBeChecked(),
    );
    resolvers.get(1)?.(jsonResponse(200, makeProduct({ id: 1, sku: "SKU-1", tracked: true })));
    await vi.waitFor(() =>
      expect(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-1" }))).toBeChecked(),
    );
    expect(screen.getByLabelText(fill(t.ui.trackToggle, { sku: "SKU-2" }))).toBeChecked();
  });
});

describe("ProductsTable: поиск и фильтры (Т31)", () => {
  beforeEach(() => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, page(rows(1), 120))));
  });

  it("первая загрузка — limit=50, offset=0", async () => {
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(lastList()).toBe(`/api/shops/${SHOP}/products?limit=50&offset=0`);
  });

  it("поиск по Enter передаёт q", async () => {
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    const input = screen.getByLabelText(t.ui.searchLabel);
    expect(input).toHaveAttribute("maxlength", "100");
    fireEvent.change(input, { target: { value: "кабель" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);
    await vi.waitFor(() => expect(lastList()).toContain("q=%D0%BA%D0%B0%D0%B1%D0%B5%D0%BB%D1%8C"));
    expect(new URL(lastList(), "http://x").searchParams.get("q")).toBe("кабель");
    expect(lastList()).toContain("limit=50&offset=0");
  });

  it.each([
    [t.ui.filterTracked, "true", "tracked=true"],
    [t.ui.filterHasBounds, "false", "has_bounds=false"],
    [t.ui.filterHasRefusal, "true", "has_refusal=true"],
  ])("фильтр «%s» → %s", async (label, value, param) => {
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    await vi.waitFor(() => expect(lastList()).toContain(param));
  });

  it("«Все» убирает параметр", async () => {
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.change(screen.getByLabelText(t.ui.filterTracked), { target: { value: "true" } });
    await vi.waitFor(() => expect(lastList()).toContain("tracked=true"));
    fireEvent.change(screen.getByLabelText(t.ui.filterTracked), { target: { value: "" } });
    await vi.waitFor(() => expect(lastList()).not.toContain("tracked"));
  });

  it("со страницы 2 смена фильтра сбрасывает offset", async () => {
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await vi.waitFor(() => expect(lastList()).toContain("offset=50"));
    fireEvent.change(screen.getByLabelText(t.ui.filterTracked), { target: { value: "true" } });
    await vi.waitFor(() => expect(lastList()).toContain("tracked=true"));
    expect(lastList()).toContain("offset=0");
  });
});

describe("ProductsTable: страницы (Т32) и пустой каталог", () => {
  it("1–50 из 120, «Назад» заблокирована; «Далее» → offset=50; на offset 100 «Далее» заблокирована", async () => {
    fetchMock.mockImplementation((url: string) => {
      const offset = Number(new URL(url, "http://x").searchParams.get("offset"));
      return Promise.resolve(
        jsonResponse(200, page(rows(Math.min(50, 120 - offset), offset + 1), 120)),
      );
    });
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(screen.getByText(fill(t.ui.pageRange, { a: 1, b: 50, total: 120 }))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t.ui.pagePrev })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await screen.findByText("SKU-51");
    expect(lastList()).toContain("offset=50");
    expect(screen.getByText(fill(t.ui.pageRange, { a: 51, b: 100, total: 120 }))).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await screen.findByText("SKU-101");
    expect(screen.getByRole("button", { name: t.ui.pageNext })).toBeDisabled();
  });

  it("пока грузится следующая страница, строки прежней не показаны и их нельзя выбрать", async () => {
    let finish: (r: Response) => void = () => {};
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, page(rows(3), 120)))
      .mockReturnValueOnce(new Promise<Response>((resolve) => (finish = resolve)));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await vi.waitFor(() => expect(lastList()).toContain("offset=50"));
    expect(screen.queryByText("SKU-1")).toBeNull();
    expect(screen.queryByLabelText(t.ui.selectAll)).toBeNull();
    expect(screen.getByRole("button", { name: t.ui.refreshing })).toBeDisabled();
    finish(jsonResponse(200, page(rows(1, 51), 120)));
    await screen.findByText("SKU-51");
  });

  it("опустевшая страница не пишет «51–50»", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, page(rows(1), 120)))
      .mockResolvedValue(jsonResponse(200, page([], 50)));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    expect(await screen.findByText(t.ui.noProducts)).toBeInTheDocument();
    expect(screen.queryByText(/51–50/)).toBeNull();
  });

  it("total 0 без фильтров — «Каталог не загружен» и ссылка на магазин", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page([], 0)));
    render(<ProductsTable shopId={SHOP} />);
    expect(await screen.findByText(t.ui.catalogEmpty)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: t.ui.toShop })).toHaveAttribute("href", `/shops/${SHOP}`);
  });

  it("total 0 с фильтром — «Нет товаров»", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, page(rows(1), 1)))
      .mockResolvedValue(jsonResponse(200, page([], 0)));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.change(screen.getByLabelText(t.ui.filterTracked), { target: { value: "true" } });
    expect(await screen.findByText(t.ui.noProducts)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.catalogEmpty)).toBeNull();
  });
});

describe("ProductsTable: массовая правка (Т33, Т34)", () => {
  function setup() {
    fetchMock.mockImplementation((url: string, init: RequestInit) =>
      Promise.resolve(
        init.method === "PATCH"
          ? jsonResponse(200, { updated: 2 })
          : jsonResponse(200, page([makeProduct({ id: 7, sku: "S7" }), makeProduct({ id: 9, sku: "S9" }), makeProduct({ id: 11, sku: "S11" })], 120)),
      ),
    );
  }
  const pick = (sku: string) =>
    fireEvent.click(screen.getByLabelText(fill(t.ui.selectRow, { sku })));
  const bulkCalls = () =>
    fetchMock.mock.calls.filter((c) => (c[1] as RequestInit).method === "PATCH");

  const actions: [string, Record<string, boolean>][] = [
    [t.ui.bulkTrackOn, { tracked: true }],
    [t.ui.bulkTrackOff, { tracked: false }],
    [t.ui.bulkDecreaseOn, { auto_decrease: true }],
    [t.ui.bulkDecreaseOff, { auto_decrease: false }],
    [t.ui.bulkRaiseOn, { auto_raise: true }],
    [t.ui.bulkRaiseOff, { auto_raise: false }],
  ];

  it("Выбрано: 2; каждое действие — тело с одним полем; после успеха выбор снят, список перезапрошен", async () => {
    setup();
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("S7");
    expect(screen.queryByText(fill(t.ui.bulkSelected, { n: 2 }))).toBeNull();
    pick("S7");
    pick("S9");
    expect(screen.getByText(fill(t.ui.bulkSelected, { n: 2 }))).toBeInTheDocument();

    for (const [label, changes] of actions) {
      const before = bulkCalls().length;
      const listsBefore = listUrls().length;
      fireEvent.click(screen.getByRole("button", { name: label }));
      await vi.waitFor(() => expect(bulkCalls().length).toBe(before + 1));
      const call = bulkCalls().at(-1) as unknown[];
      expect(call[0]).toBe(`/api/shops/${SHOP}/products`);
      expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({
        product_ids: [7, 9],
        changes,
      });
      await vi.waitFor(() => expect(listUrls().length).toBeGreaterThan(listsBefore));
      expect(screen.queryByText(fill(t.ui.bulkSelected, { n: 2 }))).toBeNull();
      expect(screen.getByText(fill(t.ui.bulkDone, { n: 2 }))).toBeInTheDocument();
      pick("S7");
      pick("S9");
    }
  });

  it("шаг 5 → changes {step:5}; ключи changes — ровно один из допустимых, без границ", async () => {
    setup();
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("S7");
    pick("S7");
    pick("S9");
    fireEvent.change(screen.getByLabelText(t.ui.bulkStepLabel), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: t.ui.bulkStepApply }));
    await vi.waitFor(() => expect(bulkCalls().length).toBe(1));
    expect(sentBody(fetchMock, fetchMock.mock.calls.indexOf(bulkCalls()[0]))).toEqual({
      product_ids: [7, 9],
      changes: { step: 5 },
    });

    // свойство для всех 7 действий
    pick("S7");
    for (const [label] of actions) {
      fireEvent.click(screen.getByRole("button", { name: label }));
      await vi.waitFor(() => expect(screen.queryByText(fill(t.ui.bulkSelected, { n: 1 }))).toBeNull());
      pick("S7");
    }
    for (const call of bulkCalls()) {
      const keys = Object.keys(
        (JSON.parse((call[1] as RequestInit).body as string) as { changes: object }).changes,
      );
      expect(keys).toHaveLength(1);
      expect(["tracked", "auto_decrease", "auto_raise", "step"]).toContain(keys[0]);
    }
  });

  it.each([
    ["0", t.ui.range],
    ["1.5", t.ui.invalidInt],
    ["", t.ui.required],
    ["100000001", t.ui.range],
  ])("шаг «%s» — запрос не уходит, текст у поля", async (value, text) => {
    setup();
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("S7");
    pick("S7");
    const calls = fetchMock.mock.calls.length;
    fireEvent.change(screen.getByLabelText(t.ui.bulkStepLabel), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: t.ui.bulkStepApply }));
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(fetchMock.mock.calls.length).toBe(calls);
  });

  it("«выбрать все на странице» выбирает все строки; смена страницы снимает выбор", async () => {
    setup();
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("S7");
    fireEvent.click(screen.getByLabelText(t.ui.selectAll));
    expect(screen.getByText(fill(t.ui.bulkSelected, { n: 3 }))).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await vi.waitFor(() => expect(lastListHas("offset=50")).toBe(true));
    expect(screen.queryByText(fill(t.ui.bulkSelected, { n: 3 }))).toBeNull();
  });

  it("404 — текст «Часть товаров не найдена, ничего не изменено»", async () => {
    fetchMock.mockImplementation((url: string, init: RequestInit) =>
      Promise.resolve(
        init.method === "PATCH"
          ? jsonResponse(404, { code: "not_found" })
          : jsonResponse(200, page([makeProduct({ id: 7, sku: "S7" })])),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("S7");
    pick("S7");
    fireEvent.click(screen.getByRole("button", { name: t.ui.bulkTrackOn }));
    expect(await screen.findByText(t.ui.bulkNotFound)).toBeInTheDocument();
  });

  function lastListHas(part: string) {
    return lastList().includes(part);
  }
});

describe("ProductsTable: вид таблицы (Т22)", () => {
  const tracked = { tracked: true, min_price: 100, max_price: 200, decision_reason: "undercut_first" } as const;

  it("тон состояния в ячейке «Проверка», «Отслеживать» и «Отказ»", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        200,
        page([
          makeProduct({ ...tracked, id: 1, sku: "T-1", position: 1 }),
          makeProduct({ ...tracked, id: 2, sku: "T-2", min_price: null }),
          makeProduct({ ...tracked, id: 3, sku: "T-3", decision_reason: null }),
          makeProduct({ id: 4, sku: "T-4", tracked: false }),
          makeProduct({ ...tracked, id: 5, sku: "T-5", refusal_reason: "kaspi_rejected" }),
        ]),
      ),
    );
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("T-1");
    const tone = (sku: string, column: string) =>
      cell(sku, column).querySelector("[data-tone]")?.getAttribute("data-tone") ?? null;
    expect(tone("T-1", t.ui.colCheck)).toBe("first");
    expect(tone("T-2", t.ui.colCheck)).toBe("nobounds");
    expect(tone("T-3", t.ui.colCheck)).toBe("pending");
    expect(tone("T-4", t.ui.colCheck)).toBeNull();
    expect(tone("T-4", t.ui.colTrack)).toBe("untracked");
    expect(tone("T-1", t.ui.colTrack)).toBeNull();
    expect(tone("T-5", t.ui.colRefusal)).toBe("problem");
  });

  it("цифровые колонки имеют класс num", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page([makeProduct({ ...tracked, sku: "N-1" })])));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("N-1");
    for (const column of [
      t.ui.colMin,
      t.ui.colMax,
      t.ui.colStep,
      t.ui.colPrice,
      t.ui.colPosition,
      t.ui.colCompetitors,
      t.ui.colFirstPrice,
    ]) {
      expect(cell("N-1", column)).toHaveClass("num");
    }
  });

  it("выбранная строка помечена data-selected", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, page(rows(2))));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByLabelText(fill(t.ui.selectRow, { sku: "SKU-1" })));
    expect(row("SKU-1")).toHaveAttribute("data-selected", "true");
    expect(row("SKU-2")).not.toHaveAttribute("data-selected");
  });
});

describe("ProductsTable: «Обновить» (Т47)", () => {
  it("на странице 2 с фильтром — повторный запрос с теми же параметрами", async () => {
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, page(rows(1), 120))));
    render(<ProductsTable shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.change(screen.getByLabelText(t.ui.filterTracked), { target: { value: "true" } });
    await vi.waitFor(() => expect(lastList()).toContain("tracked=true"));
    fireEvent.click(await screen.findByRole("button", { name: t.ui.pageNext }));
    await vi.waitFor(() => expect(lastList()).toContain("offset=50"));
    await screen.findByRole("button", { name: t.ui.refresh });
    const last = lastList();
    const count = listUrls().length;
    fireEvent.click(screen.getByRole("button", { name: t.ui.refresh }));
    await vi.waitFor(() => expect(listUrls().length).toBe(count + 1));
    expect(lastList()).toBe(last);
    expect(last).toContain("tracked=true");
  });
});
