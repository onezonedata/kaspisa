import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Product } from "@/api/types";
import { t } from "@/i18n";
import { makeProduct } from "@/test/fixtures";
import { emptyResponse, jsonResponse, sentBody, stubFetch } from "@/test/http";
import { ProductSettingsForm, productPatch, type ProductFormInput } from "./ProductSettingsForm";

let fetchMock: ReturnType<typeof stubFetch>;
beforeEach(() => {
  fetchMock = stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

function Harness({ initial, next }: { initial: Product; next?: Product }) {
  const [product, setProduct] = useState(initial);
  return (
    <>
      <ProductSettingsForm product={product} onProduct={setProduct} />
      {next ? <button onClick={() => setProduct(next)}>родитель</button> : null}
    </>
  );
}

const save = () => screen.getByRole("button", { name: t.ui.save });
const change = (label: string, value: string) =>
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
const url = (id = makeProduct().id) => `/api/shops/${makeProduct().shop_id}/products/${id}`;

describe("ProductSettingsForm: частичный PATCH (Т36)", () => {
  it("без изменений «Сохранить» заблокирована", () => {
    render(<Harness initial={makeProduct()} />);
    expect(save()).toBeDisabled();
  });

  it("изменён только max — тело {max_price:2000}", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeProduct({ max_price: 2000 })));
    render(<Harness initial={makeProduct()} />);
    change(t.ui.maxPriceLabel, "2000");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toBe(url());
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("PATCH");
    expect(sentBody(fetchMock)).toEqual({ max_price: 2000 });
  });

  it("очищенный min — явный null", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeProduct()));
    render(<Harness initial={makeProduct({ min_price: 1000 })} />);
    change(t.ui.minPriceLabel, "");
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ min_price: null });
  });

  it("флажок уходит булевым значением, одним полем", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeProduct({ tracked: true })));
    render(<Harness initial={makeProduct()} />);
    fireEvent.click(screen.getByLabelText(t.ui.trackedLabel));
    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(sentBody(fetchMock)).toEqual({ tracked: true });
  });

  it("min 2000, max 1000 — текст, fetch не вызван", () => {
    render(<Harness initial={makeProduct()} />);
    change(t.ui.minPriceLabel, "2000");
    change(t.ui.maxPriceLabel, "1000");
    fireEvent.click(save());
    expect(screen.getByText(t.errors.min_above_max)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("только min 3000 при исходном max 2000 — текст, fetch не вызван", () => {
    render(<Harness initial={makeProduct({ max_price: 2000 })} />);
    change(t.ui.minPriceLabel, "3000");
    fireEvent.click(save());
    expect(screen.getByText(t.errors.min_above_max)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("step 0 — текст у поля; step пусто — «Укажите значение»; fetch не вызван", () => {
    render(<Harness initial={makeProduct()} />);
    change(t.ui.stepLabel, "0");
    fireEvent.click(save());
    expect(screen.getByText(t.ui.range)).toBeInTheDocument();
    change(t.ui.stepLabel, "");
    fireEvent.click(save());
    expect(screen.getByText(t.ui.required)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("подсказка про границы при tracked без границ", () => {
    render(<Harness initial={makeProduct({ tracked: true, max_price: 2000 })} />);
    expect(screen.getByText(t.ui.boundsHint)).toBeInTheDocument();
  });

  it("при tracked и обеих границах подсказки нет", () => {
    render(<Harness initial={makeProduct({ tracked: true, min_price: 1, max_price: 2000 })} />);
    expect(screen.queryByText(t.ui.boundsHint)).toBeNull();
  });
});

describe("ProductSettingsForm: сервер отказал (Т36а)", () => {
  it("клиент пропустил, сервер 422 min_above_max — форма и исходный объект не меняются", async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, { code: "min_above_max" }));
    render(<Harness initial={makeProduct({ max_price: 2000 })} />);
    change(t.ui.minPriceLabel, "1500");
    fireEvent.click(save());
    expect(await screen.findByText(t.errors.min_above_max)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sentBody(fetchMock)).toEqual({ min_price: 1500 });
    expect(screen.getByLabelText(t.ui.minPriceLabel)).toHaveValue("1500");
    expect(save()).toBeEnabled();

    fireEvent.click(save());
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(sentBody(fetchMock, 1)).toEqual({ min_price: 1500 });
  });

  it("409 — показан текст, значения остаются", async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { code: "operation_not_allowed" }));
    render(<Harness initial={makeProduct()} />);
    change(t.ui.stepLabel, "5");
    fireEvent.click(save());
    expect(await screen.findByText(t.errors.operation_not_allowed)).toBeInTheDocument();
    expect(screen.getByLabelText(t.ui.stepLabel)).toHaveValue("5");
  });
});

describe("ProductSettingsForm: переинициализация (Т48)", () => {
  it("после успеха поле из ответа, «Сохранить» снова заблокирована", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, makeProduct({ step: 5 })));
    render(<Harness initial={makeProduct({ step: 1 })} />);
    change(t.ui.stepLabel, "5");
    fireEvent.click(save());
    await vi.waitFor(() => expect(save()).toBeDisabled());
    expect(screen.getByLabelText(t.ui.stepLabel)).toHaveValue("5");
  });

  it("новый объект от родителя сбрасывает несохранённые правки", () => {
    render(
      <Harness
        initial={makeProduct({ max_price: 2500 })}
        next={makeProduct({ max_price: 1800 })}
      />,
    );
    change(t.ui.maxPriceLabel, "2000");
    expect(save()).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "родитель" }));
    expect(screen.getByLabelText(t.ui.maxPriceLabel)).toHaveValue("1800");
    expect(save()).toBeDisabled();
  });
});

describe("productPatch: свойство границ (Т43)", () => {
  const originals: [number | null, number | null][] = [
    [null, null],
    [null, 1000],
    [null, 2000],
    [1000, null],
    [1000, 1000],
    [1000, 2000],
    [2000, null],
    [2000, 2000],
  ];
  // undefined — «без изменений»
  const inputs: (string | undefined)[] = [undefined, "", "500", "1000", "1500", "2000", "3000", "1e3"];

  it("512 сочетаний: тело со слитыми min > max не возвращается ни в одном случае", () => {
    let cases = 0;
    for (const [oMin, oMax] of originals) {
      const product = makeProduct({ min_price: oMin, max_price: oMax });
      for (const inMin of inputs) {
        for (const inMax of inputs) {
          cases += 1;
          const input: ProductFormInput = {
            tracked: product.tracked,
            autoDecrease: product.auto_decrease,
            autoRaise: product.auto_raise,
            minPrice: inMin ?? (oMin === null ? "" : String(oMin)),
            maxPrice: inMax ?? (oMax === null ? "" : String(oMax)),
            step: String(product.step),
          };
          const { patch, errors } = productPatch(product, input);
          const label = JSON.stringify({ oMin, oMax, inMin, inMax });
          const hasErrors = Object.keys(errors).length > 0;
          const keys = Object.keys(patch);

          const parsed = (raw: string | undefined, orig: number | null) => {
            if (raw === undefined) return { ok: true as const, value: orig };
            if (raw === "") return { ok: true as const, value: null };
            if (!/^[0-9]+$/.test(raw)) return { ok: false as const };
            return { ok: true as const, value: Number(raw) };
          };
          const pMin = parsed(inMin, oMin);
          const pMax = parsed(inMax, oMax);

          if (!pMin.ok || !pMax.ok) {
            // (а) не разбирается
            expect(hasErrors, label).toBe(true);
            expect(keys, label).toEqual([]);
            continue;
          }
          const lo = pMin.value;
          const hi = pMax.value;
          if (lo !== null && hi !== null && lo > hi && (lo !== oMin || hi !== oMax)) {
            // (б) слитые min > max
            expect(errors.bounds, label).toBe(t.errors.min_above_max);
            expect(keys, label).toEqual([]);
            continue;
          }
          expect(hasErrors, label).toBe(false);
          if (lo === oMin && hi === oMax) {
            // (в) без изменений
            expect(keys, label).toEqual([]);
            continue;
          }
          // (г) тело ровно из изменённых границ
          const expected: Record<string, number | null> = {};
          if (lo !== oMin) expected.min_price = lo;
          if (hi !== oMax) expected.max_price = hi;
          expect(patch, label).toEqual(expected);
          const mergedLo = "min_price" in patch ? patch.min_price : oMin;
          const mergedHi = "max_price" in patch ? patch.max_price : oMax;
          expect(
            mergedLo != null && mergedHi != null && mergedLo > mergedHi,
            label,
          ).toBe(false);
        }
      }
    }
    expect(cases).toBe(512);
  });
});

describe("ProductSettingsForm: повторный пароль (Т30)", () => {
  const CONFIRM = "/api/auth/confirm-password";
  const required = () => jsonResponse(403, { code: "password_required" });
  const lower = () => {
    render(<Harness initial={makeProduct({ min_price: 1000, max_price: 5000 })} />);
    change(t.ui.minPriceLabel, "500");
    fireEvent.click(save());
  };
  const typePassword = (value: string) =>
    fireEvent.change(screen.getByLabelText(t.ui.password), { target: { value } });
  const confirm = () => fireEvent.click(screen.getByRole("button", { name: t.ui.passwordConfirm }));
  const urls = () => fetchMock.mock.calls.map((c) => `${(c[1] as RequestInit).method} ${c[0]}`);

  it("403 password_required — форма пароля; верный пароль — правка повторена один раз", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(emptyResponse(204))
      .mockResolvedValueOnce(jsonResponse(200, makeProduct({ min_price: 500, max_price: 5000 })));
    lower();
    expect(await screen.findByText(t.ui.passwordTitle)).toBeInTheDocument();
    typePassword("секрет-пароль");
    confirm();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(urls()).toEqual([`PATCH ${url()}`, `POST ${CONFIRM}`, `PATCH ${url()}`]);
    expect(sentBody(fetchMock, 1)).toEqual({ password: "секрет-пароль" });
    expect(sentBody(fetchMock, 2)).toEqual({ min_price: 500 });
    await vi.waitFor(() => expect(screen.queryByText(t.ui.passwordTitle)).toBeNull());
    expect(await screen.findByText(t.ui.saved)).toBeInTheDocument();
  });

  it("password_invalid — «Неверный пароль», форма остаётся, повтора правки нет", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(jsonResponse(403, { code: "password_invalid" }));
    lower();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("не тот");
    confirm();
    expect(await screen.findByText(t.errors.password_invalid)).toBeInTheDocument();
    expect(screen.getByText(t.ui.passwordTitle)).toBeInTheDocument();
    expect(urls()).toEqual([`PATCH ${url()}`, `POST ${CONFIRM}`]);
  });

  it("«Отмена» — форма закрыта, повтора нет, пароль не остаётся в поле", async () => {
    fetchMock.mockResolvedValueOnce(required()).mockResolvedValueOnce(required());
    lower();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("секрет");
    fireEvent.click(screen.getByRole("button", { name: t.ui.passwordCancel }));
    expect(screen.queryByText(t.ui.passwordTitle)).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(save());
    await screen.findByText(t.ui.passwordTitle);
    expect(screen.getByLabelText(t.ui.password)).toHaveValue("");
  });

  it("повтор снова password_required — форма снова, третьего запроса правки нет", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(emptyResponse(204))
      .mockResolvedValueOnce(required());
    lower();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("секрет");
    confirm();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(await screen.findByText(t.ui.passwordTitle)).toBeInTheDocument();
    expect(screen.getByLabelText(t.ui.password)).toHaveValue("");
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("429 — время ожидания и совет «Выйти на всех устройствах»", async () => {
    fetchMock
      .mockResolvedValueOnce(required())
      .mockResolvedValueOnce(jsonResponse(429, { code: "too_many_attempts", retry_after_seconds: 600 }));
    lower();
    await screen.findByText(t.ui.passwordTitle);
    typePassword("секрет");
    confirm();
    const alert = await screen.findByText(new RegExp(t.errors.too_many_attempts));
    expect(alert.textContent).toContain("10 мин");
    expect(screen.getByText(t.ui.passwordLockedHint)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("другая ошибка правки — обычный текст, формы пароля нет", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(404, { code: "not_found" }));
    lower();
    expect(await screen.findByText(t.errors.not_found)).toBeInTheDocument();
    expect(screen.queryByText(t.ui.passwordTitle)).toBeNull();
  });
});
