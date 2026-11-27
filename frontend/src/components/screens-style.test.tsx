import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makePriceChange, makeProduct, makeShop } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { DeleteShopButton } from "./DeleteShopButton";
import { EventsPage } from "./EventsPage";
import { HaltBanner } from "./HaltBanner";
import { ProductPage } from "./ProductPage";
import { WriteBanner } from "./WriteBanner";

const P = makeProduct();
vi.mock("next/navigation", () => ({
  useParams: () => ({ shopId: P.shop_id, productId: String(P.id) }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

// Вид остальных экранов этапа 22 (часть 4): разметка, от которой зависят стили, не теряется.
describe("экраны в едином стиле (Т37)", () => {
  it("журнал действий: таблица лежит в прокручиваемой рамке", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, [
        {
          id: 1,
          kind: "login",
          ip: "203.0.113.7",
          shop_id: null,
          sku: null,
          old_value: null,
          new_value: null,
          created_at: "2026-11-19T10:00:00Z",
        },
      ]),
    );
    const { container } = render(<EventsPage />);
    await screen.findByRole("table");
    expect(container.querySelector(".table-scroll > table")).not.toBeNull();
  });

  it("карточка товара: цифры — .num, тон проверки и отказа, разделы — карточки", async () => {
    fetchMock.mockImplementation((url: unknown) =>
      Promise.resolve(
        String(url).includes("/price-changes")
          ? jsonResponse(200, { items: [makePriceChange({ product_id: P.id })] })
          : jsonResponse(
              200,
              makeProduct({ tracked: true, min_price: null, refusal_reason: "no_reason_given" }),
            ),
      ),
    );
    const { container } = render(<ProductPage />);
    await screen.findByRole("heading", { name: P.title });
    for (const field of ["current_price", "position", "competitors_count", "first_place_price"]) {
      expect(container.querySelector(`[data-field='${field}']`)).toHaveClass("num");
    }
    expect(container.querySelector("dl.product-fields [data-tone='nobounds']")).not.toBeNull();
    expect(container.querySelector("dl.product-fields [data-tone='problem']")).not.toBeNull();
    expect(container.querySelectorAll("section.card").length).toBeGreaterThanOrEqual(3);
    await screen.findByRole("table");
    expect(container.querySelector(".table-scroll")).not.toBeNull();
  });

  it("баннеры остановки и записи различаются по смыслу", () => {
    const halted = render(
      <HaltBanner shop={makeShop({ halt_reason: "login_errors" })} onShop={() => {}} />,
    );
    expect(halted.container.querySelector(".banner.tone-danger")).not.toBeNull();
    halted.unmount();
    const write = render(<WriteBanner shop={makeShop({ server_dry_run: true })} />);
    expect(write.container.querySelector(".banner.tone-warn")).not.toBeNull();
  });

  it("удаление магазина — опасная кнопка в своей карточке", () => {
    const { container } = render(<DeleteShopButton shopId="s1" />);
    expect(container.querySelector("section.card")).not.toBeNull();
    expect(screen.getByRole("button", { name: t.ui.deleteShop })).toHaveClass("btn-danger");
  });
});
