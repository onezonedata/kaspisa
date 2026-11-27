import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { t } from "@/i18n";
import { emptyResponse, jsonResponse, stubFetch } from "@/test/http";
import { DeleteShopButton } from "./DeleteShopButton";

const router = vi.hoisted(() => ({ replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
  router.push.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const click = () => fireEvent.click(screen.getByRole("button", { name: t.ui.deleteShop }));

describe("DeleteShopButton (Т27)", () => {
  it("отказ в подтверждении — запроса нет", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<DeleteShopButton shopId="s1" />);
    click();
    expect(confirm).toHaveBeenCalledWith(t.ui.confirmDeleteShop);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("согласие — DELETE и переход к списку", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValue(emptyResponse(204));
    render(<DeleteShopButton shopId="s1" />);
    click();
    await vi.waitFor(() => expect(router.push).toHaveBeenCalledWith("/shops"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/shops/s1");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("409 price_write_open — текст, перехода нет", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockResolvedValue(
      jsonResponse(409, { code: "price_write_open", retry_after_seconds: 125 }),
    );
    render(<DeleteShopButton shopId="s1" />);
    click();
    expect(await screen.findByRole("alert")).toHaveTextContent("2 мин 5 с");
    expect(router.push).not.toHaveBeenCalled();
  });
});
