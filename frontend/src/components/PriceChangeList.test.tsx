import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fill, journalErrorText, refusalText, t } from "@/i18n";
import { formatPrice } from "@/lib/format";
import { makePriceChange } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { PriceChangeList } from "./PriceChangeList";

const SHOP = "11111111-1111-4111-8111-111111111111";

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const urls = () => fetchMock.mock.calls.map((c) => String(c[0]));
const rows = (n: number, from = 1) =>
  Array.from({ length: n }, (_, i) => makePriceChange({ id: from + i, sku: `SKU-${from + i}` }));
const reply = (items: ReturnType<typeof rows>) =>
  fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, { items })));

describe("PriceChangeList: строка (Т39)", () => {
  it("показывает цены, причину, подробности и результат", async () => {
    reply([
      makePriceChange({
        old_price: 1500,
        new_price: 1199,
        reason: "undercut_first",
        competitor_name: "Конкурент",
        competitor_merchant_id: "M1",
        competitor_price: 1200,
        target_price: 1199,
        capped_at_max: true,
        skipped_competitors: 2,
        cities_aligned: true,
        result: "dry_run",
      }),
    ]);
    render(<PriceChangeList shopId={SHOP} />);
    expect(await screen.findByText(/1\s500\s₸ → 1\s199\s₸/)).toBeInTheDocument();
    expect(screen.getByText(t.reason.undercut_first)).toBeInTheDocument();
    expect(screen.getByText(/Конкурент \(M1\), цена 1\s200\s₸/)).toBeInTheDocument();
    expect(screen.getByText(/целевая 1\s199\s₸/)).toBeInTheDocument();
    expect(screen.getByText(t.ui.cappedAtMax)).toBeInTheDocument();
    expect(screen.getByText(fill(t.ui.skippedCompetitors, { n: 2 }))).toBeInTheDocument();
    expect(screen.getByText(t.ui.citiesAligned)).toBeInTheDocument();
    expect(screen.getByText(t.result.dry_run)).toBeInTheDocument();
  });

  it("без подробностей лишних строк нет", async () => {
    reply([makePriceChange({ target_price: null })]);
    render(<PriceChangeList shopId={SHOP} />);
    await screen.findByText(t.reason.undercut_first);
    expect(screen.queryByText(t.ui.cappedAtMax)).toBeNull();
    expect(screen.queryByText(t.ui.citiesAligned)).toBeNull();
    expect(screen.queryByText(/отброшено/)).toBeNull();
    expect(screen.queryByText(/целевая/)).toBeNull();
  });

  it("строка no_change: «цена — без изменений», причина и результат (Т60)", async () => {
    reply([
      makePriceChange({
        old_price: 600,
        new_price: 600,
        reason: "at_min_price",
        target_price: 600,
        result: "no_change",
      }),
    ]);
    render(<PriceChangeList shopId={SHOP} />);
    expect(await screen.findByText(fill(t.ui.priceUnchanged, { price: formatPrice(600) }))).toBeInTheDocument();
    expect(screen.queryByText(/→/)).toBeNull();
    expect(screen.getByText(t.reason.at_min_price)).toBeInTheDocument();
    expect(screen.getByText("цена не изменилась")).toBeInTheDocument();
  });

  it("пустой журнал — «Изменений цены ещё не было»", async () => {
    reply([]);
    render(<PriceChangeList shopId={SHOP} />);
    expect(await screen.findByText(t.ui.journalEmpty)).toBeInTheDocument();
  });
});

describe("PriceChangeList: незнакомые коды (Т40)", () => {
  it("запасные тексты с кодами, без падения", async () => {
    reply([makePriceChange({ reason: "new_rule", result: "weird", error: "unknown_err" })]);
    render(<PriceChangeList shopId={SHOP} />);
    expect(await screen.findByText(`${t.common.unknownCode} (new_rule)`)).toBeInTheDocument();
    expect(screen.getByText(`${t.common.unknownCode} (weird)`)).toBeInTheDocument();
    expect(screen.getByText(`${t.common.unknownCode} (unknown_err)`)).toBeInTheDocument();
  });

  it("failed с permanent:preorder — текст отказа с пометкой", async () => {
    reply([makePriceChange({ result: "failed", error: "permanent:preorder" })]);
    render(<PriceChangeList shopId={SHOP} />);
    expect(await screen.findByText(t.result.failed)).toBeInTheDocument();
    expect(screen.getByText(refusalText("permanent:preorder"))).toBeInTheDocument();
    expect(journalErrorText("permanent:preorder")).toContain(t.refusal.permanentNote);
  });
});

describe("PriceChangeList: страницы и ссылки (Т41)", () => {
  it("50 строк — «Далее» активна, нажатие даёт offset=50", async () => {
    reply(rows(50));
    render(<PriceChangeList shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(urls()[0]).toContain("limit=50");
    expect(urls()[0]).toContain("offset=0");
    expect(screen.getByRole("button", { name: t.ui.pagePrev })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await vi.waitFor(() => expect(urls().at(-1)).toContain("offset=50"));
    expect(await screen.findByRole("button", { name: t.ui.pagePrev })).toBeEnabled();
  });

  it("12 строк — «Далее» disabled", async () => {
    reply(rows(12));
    render(<PriceChangeList shopId={SHOP} />);
    await screen.findByText("SKU-1");
    expect(screen.getByRole("button", { name: t.ui.pageNext })).toBeDisabled();
  });

  it("с productId запрос содержит product_id и ссылки на карточку нет", async () => {
    reply(rows(1));
    render(<PriceChangeList shopId={SHOP} productId={5} />);
    await screen.findByText(t.reason.undercut_first);
    expect(urls()[0]).toContain("product_id=5");
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("без productId есть ссылка на карточку товара", async () => {
    reply([makePriceChange({ product_id: 7, title: "Чехол" })]);
    render(<PriceChangeList shopId={SHOP} />);
    const link = await screen.findByRole("link", { name: "Чехол" });
    expect(link).toHaveAttribute("href", `/shops/${SHOP}/products/7`);
    expect(urls()[0]).not.toContain("product_id");
  });
});

describe("PriceChangeList: «Обновить» (Т47)", () => {
  it("повторный запрос с тем же offset", async () => {
    reply(rows(50));
    render(<PriceChangeList shopId={SHOP} />);
    await screen.findByText("SKU-1");
    fireEvent.click(screen.getByRole("button", { name: t.ui.pageNext }));
    await vi.waitFor(() => expect(urls().at(-1)).toContain("offset=50"));
    await screen.findByRole("button", { name: t.ui.refresh });
    const count = urls().length;
    fireEvent.click(screen.getByRole("button", { name: t.ui.refresh }));
    await vi.waitFor(() => expect(urls().length).toBe(count + 1));
    expect(urls().at(-1)).toContain("offset=50");
  });

  it("ошибка загрузки показывается", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { code: "not_found" }));
    render(<PriceChangeList shopId={SHOP} />);
    expect(await screen.findByText(t.errors.not_found)).toBeInTheDocument();
  });
});
