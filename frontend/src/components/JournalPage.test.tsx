import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makePriceChange } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { JournalPage } from "./JournalPage";

const SHOP = "11111111-1111-4111-8111-111111111111";
vi.mock("next/navigation", () => ({ useParams: () => ({ shopId: SHOP }) }));

beforeEach(() => {
  stubFetch().mockResolvedValue(jsonResponse(200, { items: [makePriceChange()] }));
});
afterEach(() => vi.unstubAllGlobals());

describe("JournalPage", () => {
  it("заголовок, ссылка на магазин и журнал магазина без product_id", async () => {
    render(<JournalPage />);
    expect(screen.getByRole("heading", { name: t.ui.journalTitle })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: t.ui.toShop })).toHaveAttribute("href", `/shops/${SHOP}`);
    expect(await screen.findByText(t.reason.undercut_first)).toBeInTheDocument();
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toBe(
      `/api/shops/${SHOP}/price-changes?limit=50&offset=0`,
    );
  });
});
