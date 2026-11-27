import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fill, t } from "@/i18n";
import { formatDateTime } from "@/lib/format";
import { makeShop } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { CatalogSummary } from "./CatalogSummary";

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const SYNCED = "2026-11-15T10:00:00Z";
const shop = (o = {}) =>
  makeShop({
    id: "s1",
    connection_state: "connected",
    catalog_synced_at: SYNCED,
    ...o,
  });

describe("CatalogSummary (Т26)", () => {
  it("числа, время загрузки и ссылка на отслеживаемые", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { total: 272, tracked: 1 }));
    render(<CatalogSummary shop={shop()} />);
    expect(
      await screen.findByText(
        fill(t.ui.catalogSummary, { total: 272, time: formatDateTime(SYNCED) }),
      ),
    ).toBeInTheDocument();
    const link = screen.getByRole("link", { name: fill(t.ui.trackedCount, { n: 1 }) });
    expect(link).toHaveAttribute("href", "/shops/s1/products?tracked=true");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/shops/s1/catalog-summary");
    expect(screen.getByText(/Товаров в каталоге: 272/)).toBeInTheDocument();
  });

  it("каталог не загружен — подсказка, запроса сводки нет", () => {
    render(<CatalogSummary shop={shop({ catalog_synced_at: null })} />);
    expect(screen.getByText(t.ui.catalogNotLoaded)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("каталог загружается — «Каталог загружается» в role=status", () => {
    render(<CatalogSummary shop={shop({ catalog_loading: true })} />);
    expect(screen.getByRole("status")).toHaveTextContent(t.ui.catalogLoading);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("сводка с null — «Товаров больше 10 000», ссылки нет", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { total: null, tracked: null }));
    render(<CatalogSummary shop={shop()} />);
    expect(
      await screen.findByText(fill(t.ui.catalogTooLarge, { time: formatDateTime(SYNCED) })),
    ).toBeInTheDocument();
    expect(screen.getByText(/Товаров больше 10 000/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("ошибка сводки показана внутри блока", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { code: "not_found" }));
    render(<CatalogSummary shop={shop()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.not_found);
  });
});
