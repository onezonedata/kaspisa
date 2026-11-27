import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { errorText, t } from "@/i18n";
import { makeProduct } from "@/test/fixtures";
import { jsonResponse, stubFetch } from "@/test/http";
import { PriceRead } from "./PriceRead";

const PRODUCT = makeProduct();
const URL = `/api/shops/${PRODUCT.shop_id}/products/${PRODUCT.id}/price-read`;

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const button = () => screen.getByRole("button", { name: t.ui.priceRead });

describe("PriceRead (Т81)", () => {
  it("нажатие — «Читаю…», затем onRead с товаром, итога нет", async () => {
    const result = makeProduct({ current_price: 2998, position: 3, first_place_price: 2643 });
    let answer: (r: Response) => void = () => {};
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => (answer = resolve)));
    const onRead = vi.fn();
    const { container } = render(<PriceRead shopId={PRODUCT.shop_id} product={PRODUCT} onRead={onRead} />);
    fireEvent.click(button());

    const busy = await screen.findByRole("button", { name: t.ui.priceReading });
    expect(busy).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(URL);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(init.body).toBeUndefined();
    expect((init.headers as Record<string, string>)["X-Requested-With"]).toBe("kaspisa");

    answer(jsonResponse(200, result));
    await waitFor(() => expect(onRead).toHaveBeenCalledWith(result));
    expect(button()).toBeEnabled();
    // Рядом с кнопкой нет ни цены, ни места, ни времени.
    expect(container.textContent).toBe(t.ui.priceRead);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("PriceRead: ошибки (Т36)", () => {
  it.each([
    [429, "price_read_too_early", 45],
    [429, "kaspi_busy", 120],
    [502, "kaspi_unavailable", null],
    [409, "price_read_in_progress", null],
    [409, "state_changed", null],
    [503, "service_unavailable", null],
  ])("%i %s — текст словаря, кнопка снова доступна", async (status, code, retry) => {
    fetchMock.mockResolvedValue(jsonResponse(status, { code, retry_after_seconds: retry }));
    const onRead = vi.fn();
    render(<PriceRead shopId={PRODUCT.shop_id} product={PRODUCT} onRead={onRead} />);
    fireEvent.click(button());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(errorText(new ApiError(status, code, null, retry)));
    if (retry !== null) expect(alert.textContent).toMatch(/\d+ (с|мин)/);
    expect(onRead).not.toHaveBeenCalled();
    expect(button()).toBeEnabled();
  });

  it("повторное нажатие после ошибки убирает сообщение", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(502, { code: "kaspi_unavailable" }));
    fetchMock.mockResolvedValueOnce(jsonResponse(200, makeProduct()));
    render(<PriceRead shopId={PRODUCT.shop_id} product={PRODUCT} onRead={vi.fn()} />);
    fireEvent.click(button());
    await screen.findByRole("alert");
    fireEvent.click(button());
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});
