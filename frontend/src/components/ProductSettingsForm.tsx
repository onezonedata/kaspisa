"use client";

import { useState, type FormEvent } from "react";

import { updateProduct, type ApiError } from "@/api/client";
import type { Product, ProductSettingsIn } from "@/api/types";
import { parseErrorText, t } from "@/i18n";
import { parseIntField } from "@/lib/parse";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";
import { PasswordConfirm } from "./PasswordConfirm";

export interface ProductFormInput {
  tracked: boolean;
  autoDecrease: boolean;
  autoRaise: boolean;
  minPrice: string;
  maxPrice: string;
  step: string;
}

export interface ProductFormErrors {
  minPrice?: string;
  maxPrice?: string;
  step?: string;
  bounds?: string;
}

function toInput(product: Product): ProductFormInput {
  return {
    tracked: product.tracked,
    autoDecrease: product.auto_decrease,
    autoRaise: product.auto_raise,
    minPrice: product.min_price === null ? "" : String(product.min_price),
    maxPrice: product.max_price === null ? "" : String(product.max_price),
    step: String(product.step),
  };
}

/**
 * Только изменённые поля: сервер меняет лишь присланное. Снять границу — явный null.
 * При любой ошибке патч пуст; слитые min > max (правило сервера) — ошибка, тела нет.
 */
export function productPatch(
  original: Product,
  input: ProductFormInput,
): { patch: Partial<ProductSettingsIn>; errors: ProductFormErrors } {
  const patch: Partial<ProductSettingsIn> = {};
  const errors: ProductFormErrors = {};

  if (input.tracked !== original.tracked) patch.tracked = input.tracked;
  if (input.autoDecrease !== original.auto_decrease) patch.auto_decrease = input.autoDecrease;
  if (input.autoRaise !== original.auto_raise) patch.auto_raise = input.autoRaise;

  const min = parseIntField(input.minPrice, { optional: true });
  const max = parseIntField(input.maxPrice, { optional: true });
  if (!min.ok) errors.minPrice = parseErrorText(min);
  else if (min.value !== original.min_price) patch.min_price = min.value;
  if (!max.ok) errors.maxPrice = parseErrorText(max);
  else if (max.value !== original.max_price) patch.max_price = max.value;

  const step = parseIntField(input.step, { optional: false });
  if (!step.ok) errors.step = parseErrorText(step);
  else if (step.value !== original.step) patch.step = step.value as number;

  if (!errors.minPrice && !errors.maxPrice && ("min_price" in patch || "max_price" in patch)) {
    const lo = "min_price" in patch ? patch.min_price : original.min_price;
    const hi = "max_price" in patch ? patch.max_price : original.max_price;
    if (lo != null && hi != null && lo > hi) errors.bounds = t.errors.min_above_max;
  }

  return { patch: Object.keys(errors).length > 0 ? {} : patch, errors };
}

export function ProductSettingsForm({
  product,
  onProduct,
}: {
  product: Product;
  onProduct: (product: Product) => void;
}) {
  const [input, setInput] = useState(() => toInput(product));
  const [errors, setErrors] = useState<ProductFormErrors>({});
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  // Правка, ждущая подтверждения пароля.
  const [ask, setAsk] = useState<Partial<ProductSettingsIn> | null>(null);
  // Любой новый объект-источник сбрасывает несохранённые правки.
  const [seen, setSeen] = useState(product);
  if (seen !== product) {
    setSeen(product);
    setInput(toInput(product));
    setErrors({});
  }

  // Ошибка разбора тоже считается правкой: кнопка активна, чтобы показать причину.
  const current = productPatch(product, input);
  const dirty = Object.keys(current.patch).length + Object.keys(current.errors).length > 0;
  const set = <K extends keyof ProductFormInput>(key: K, value: ProductFormInput[K]) => {
    setInput((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };
  const noBounds = input.tracked && (input.minPrice.trim() === "" || input.maxPrice.trim() === "");

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const result = productPatch(product, input);
    setErrors(result.errors);
    setError(null);
    setSaved(false);
    if (Object.keys(result.errors).length > 0 || Object.keys(result.patch).length === 0) return;
    await send(result.patch);
  }

  /** Отправляет правку; `password_required` не ошибка, а просьба ввести пароль (ADR 0024). */
  async function send(patch: Partial<ProductSettingsIn>) {
    setBusy(true);
    setAsk(null);
    try {
      onProduct(await updateProduct(product.shop_id, product.id, patch));
      setSaved(true);
    } catch (e) {
      // Ни исходный объект, ни введённые значения не меняются.
      const apiError = e as ApiError;
      if (apiError.code === "password_required") setAsk(patch);
      else setError(apiError);
    } finally {
      setBusy(false);
    }
  }

  /** После подтверждения пароля исходная правка повторяется один раз; новый отказ снова открывает форму. */
  function onConfirmed() {
    if (ask !== null) void send(ask);
  }

  return (
    <>
    <form className="card form-grid" onSubmit={onSubmit}>
      <h2>{t.ui.productSettingsTitle}</h2>
      <label className="check">
        <input
          type="checkbox"
          checked={input.tracked}
          onChange={(e) => set("tracked", e.target.checked)}
        />
        {t.ui.trackedLabel}
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={input.autoDecrease}
          onChange={(e) => set("autoDecrease", e.target.checked)}
        />
        {t.ui.autoDecreaseLabel}
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={input.autoRaise}
          onChange={(e) => set("autoRaise", e.target.checked)}
        />
        {t.ui.autoRaiseLabel}
      </label>
      <label>
        {t.ui.minPriceLabel}
        <input
          inputMode="numeric"
          value={input.minPrice}
          onChange={(e) => set("minPrice", e.target.value)}
        />
      </label>
      {errors.minPrice ? <p role="alert">{errors.minPrice}</p> : null}
      <label>
        {t.ui.maxPriceLabel}
        <input
          inputMode="numeric"
          value={input.maxPrice}
          onChange={(e) => set("maxPrice", e.target.value)}
        />
      </label>
      {errors.maxPrice ? <p role="alert">{errors.maxPrice}</p> : null}
      {errors.bounds ? <p role="alert">{errors.bounds}</p> : null}
      {noBounds ? <p>{t.ui.boundsHint}</p> : null}
      <label>
        {t.ui.stepLabel}
        <input inputMode="numeric" value={input.step} onChange={(e) => set("step", e.target.value)} />
      </label>
      {errors.step ? <p role="alert">{errors.step}</p> : null}
      <ErrorMessage error={error} />
      {saved ? <p role="status">{t.ui.saved}</p> : null}
      <BusyButton type="submit" busy={busy} busyText={t.ui.saving} disabled={!dirty}>
        {t.ui.save}
      </BusyButton>
    </form>
    {ask !== null ? <PasswordConfirm onConfirmed={onConfirmed} onCancel={() => setAsk(null)} /> : null}
    </>
  );
}
