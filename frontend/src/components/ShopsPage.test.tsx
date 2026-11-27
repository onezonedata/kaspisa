import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { makeShop } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { ShopsPage } from "./ShopsPage";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

describe("ShopsPage (Т15–Т18)", () => {
  it("Т15: пустой список — форма добавления видна", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));
    render(<ShopsPage />);
    expect(await screen.findByLabelText(t.ui.kaspiPassword)).toHaveAttribute("type", "password");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/shops");
  });

  it("Т16: один магазин — формы нет, строка магазина видна", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, [makeShop({ merchant_uid: "30000001" })]));
    render(<ShopsPage />);
    expect(await screen.findByRole("link", { name: "30000001" })).toBeInTheDocument();
    expect(screen.queryByLabelText(t.ui.kaspiPassword)).not.toBeInTheDocument();
  });

  it("Т17: пока список грузится и при ошибке загрузки — формы нет", async () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    const first = render(<ShopsPage />);
    expect(screen.queryByLabelText(t.ui.kaspiPassword)).not.toBeInTheDocument();
    first.unmount();

    fetchMock.mockReset();
    fetchMock.mockResolvedValue(jsonResponse(500, { code: "internal_error" }));
    render(<ShopsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(t.errors.internal_error);
    expect(screen.queryByLabelText(t.ui.kaspiPassword)).not.toBeInTheDocument();
  });

  it("Т18: два магазина — оба в списке, формы нет", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, [
        makeShop({ id: "a", merchant_uid: "30000001" }),
        makeShop({ id: "b", merchant_uid: "30000002" }),
      ]),
    );
    render(<ShopsPage />);
    expect(await screen.findByRole("link", { name: "30000001" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "30000002" })).toBeInTheDocument();
    expect(screen.queryByLabelText(t.ui.kaspiPassword)).not.toBeInTheDocument();
  });

  it("409 при двух магазинах: форма исчезла, ошибка лимита остаётся над списком", async () => {
    const two = [
      makeShop({ id: "a", merchant_uid: "30000001" }),
      makeShop({ id: "b", merchant_uid: "30000002" }),
    ];
    let listCalls = 0;
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return jsonResponse(409, { code: "shop_limit_reached" });
      listCalls += 1;
      return jsonResponse(200, listCalls === 1 ? [] : two);
    });
    render(<ShopsPage />);
    const login = await screen.findByLabelText(t.ui.kaspiLogin);
    fireEvent.change(login, { target: { value: "seller@example.com" } });
    fireEvent.change(screen.getByLabelText(t.ui.kaspiPassword), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: t.ui.addShop }));
    expect(await screen.findByRole("link", { name: "30000002" })).toBeInTheDocument();
    expect(screen.queryByLabelText(t.ui.kaspiPassword)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(t.errors.shop_limit_reached);
  });
});
