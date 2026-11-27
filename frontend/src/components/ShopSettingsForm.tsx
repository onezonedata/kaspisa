"use client";

import { useState, type FormEvent } from "react";

import { ApiError, updateShopSettings } from "@/api/client";
import type { BotMode, Shop, ShopSettingsIn, Strategy } from "@/api/types";
import { parseErrorText, t } from "@/i18n";
import { parseIntField } from "@/lib/parse";
import { BusyButton } from "./BusyButton";
import { ErrorMessage } from "./ErrorMessage";

export interface ShopFormInput {
  strategy: string;
  cityId: string;
  excluded: string;
  defaultStep: string;
  autoRaise: boolean;
  botMode: BotMode;
}

export interface ShopFormErrors {
  cityId?: string;
  excluded?: string;
  defaultStep?: string;
}

const MAX_EXCLUDED = 200;
/** Астана: подставляется, пока у магазина город не задан (ADR 0014, ADR 0032). */
const DEFAULT_CITY_ID = "710000000";

function toInput(shop: Shop): ShopFormInput {
  return {
    strategy: shop.strategy,
    cityId: shop.city_id ?? DEFAULT_CITY_ID,
    excluded: shop.excluded_merchant_ids.join("\n"),
    defaultStep: String(shop.default_step),
    autoRaise: shop.auto_raise_enabled,
    botMode: shop.bot_mode,
  };
}

/** Только изменённые поля: сервер меняет лишь присланное, остальное не трогаем. */
export function shopPatch(
  original: Shop,
  input: ShopFormInput,
): { patch: Partial<ShopSettingsIn>; errors: ShopFormErrors } {
  const patch: Partial<ShopSettingsIn> = {};
  const errors: ShopFormErrors = {};

  if (input.strategy !== original.strategy) patch.strategy = input.strategy as Strategy;
  if (input.autoRaise !== original.auto_raise_enabled) patch.auto_raise_enabled = input.autoRaise;
  if (input.botMode !== original.bot_mode) patch.bot_mode = input.botMode;

  const city = input.cityId.trim();
  if (city !== "" && !/^[0-9]{1,32}$/.test(city)) errors.cityId = t.ui.cityInvalid;
  else if ((city === "" ? null : city) !== original.city_id) patch.city_id = city === "" ? null : city;

  const ids = input.excluded
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
  if (ids.length > MAX_EXCLUDED) errors.excluded = t.ui.excludedTooMany;
  else if (ids.join("\n") !== original.excluded_merchant_ids.join("\n")) {
    patch.excluded_merchant_ids = ids;
  }

  const step = parseIntField(input.defaultStep, { optional: false });
  if (!step.ok) errors.defaultStep = parseErrorText(step);
  else if (step.value !== original.default_step) patch.default_step = step.value as number;

  return { patch, errors };
}

export function ShopSettingsForm({ shop, onShop }: { shop: Shop; onShop: (shop: Shop) => void }) {
  const [input, setInput] = useState(() => toInput(shop));
  const [errors, setErrors] = useState<ShopFormErrors>({});
  const [error, setError] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState(false);
  // Режим `live` только что покинут: уже отправленная цена может примениться (ADR 0024).
  const [leftLive, setLeftLive] = useState(false);
  const [busy, setBusy] = useState(false);
  // Любой новый объект-источник сбрасывает несохранённые правки.
  const [seen, setSeen] = useState(shop);
  if (seen !== shop) {
    setSeen(shop);
    setInput(toInput(shop));
    setErrors({});
  }

  // Ошибка разбора тоже считается правкой: кнопка активна, чтобы показать причину.
  const current = shopPatch(shop, input);
  const dirty = Object.keys(current.patch).length + Object.keys(current.errors).length > 0;
  const set = <K extends keyof ShopFormInput>(key: K, value: ShopFormInput[K]) => {
    setInput((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
    setLeftLive(false);
  };

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const result = shopPatch(shop, input);
    setErrors(result.errors);
    setError(null);
    setSaved(false);
    setLeftLive(false);
    if (Object.keys(result.errors).length > 0 || Object.keys(result.patch).length === 0) return;
    if (result.patch.bot_mode === "live") {
      // Переход в `live` подтверждается; пароль не нужен (ADR 0024).
      const blocked = shop.server_dry_run !== false;
      const text = blocked ? `${t.ui.liveConfirm} ${t.ui.liveConfirmServerBlocked}` : t.ui.liveConfirm;
      if (!window.confirm(text)) return;
    }
    const wasLive = shop.bot_mode === "live";
    setBusy(true);
    try {
      const updated = await updateShopSettings(shop.id, result.patch);
      onShop(updated);
      setSaved(true);
      setLeftLive(wasLive && updated.bot_mode !== "live");
    } catch (e) {
      const apiError = e as ApiError;
      const fields = apiError.fields ?? [];
      if (fields.includes("body.city_id")) {
        setErrors({ cityId: t.ui.cityRequired });
        // Остальные поля 422 не теряем: показываем их общим текстом.
        const rest = fields.filter((f) => f !== "body.city_id");
        if (rest.length > 0) {
          setError(new ApiError(apiError.status, apiError.code, rest, apiError.retryAfterSeconds));
        }
      } else setError(apiError);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={onSubmit}>
      <h2>{t.ui.settingsTitle}</h2>
      <label>
        {t.ui.strategyLabel}
        <select value={input.strategy} onChange={(e) => set("strategy", e.target.value)}>
          {Object.entries(t.strategy).map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t.ui.cityLabel}
        <input value={input.cityId} onChange={(e) => set("cityId", e.target.value)} />
      </label>
      <small>{t.ui.cityHint}</small>
      {shop.city_id === null && input.cityId.trim() === DEFAULT_CITY_ID ? (
        <small>{t.ui.cityDefaulted}</small>
      ) : null}
      {errors.cityId ? <p role="alert">{errors.cityId}</p> : null}
      <label>
        {t.ui.excludedLabel}
        <textarea
          rows={4}
          value={input.excluded}
          onChange={(e) => set("excluded", e.target.value)}
        />
      </label>
      {errors.excluded ? <p role="alert">{errors.excluded}</p> : null}
      <label>
        {t.ui.defaultStepLabel}
        <input
          inputMode="numeric"
          value={input.defaultStep}
          onChange={(e) => set("defaultStep", e.target.value)}
        />
      </label>
      {errors.defaultStep ? <p role="alert">{errors.defaultStep}</p> : null}
      <label className="check">
        <input
          type="checkbox"
          checked={input.autoRaise}
          onChange={(e) => set("autoRaise", e.target.checked)}
        />
        {t.ui.autoRaiseLabel}
      </label>
      <fieldset>
        <legend>{t.ui.botModeLabel}</legend>
        {(["off", "dry_run", "live"] as const).map((mode) => (
          <label key={mode} className="check">
            <input
              type="radio"
              name="bot_mode"
              value={mode}
              checked={input.botMode === mode}
              onChange={() => set("botMode", mode)}
            />
            {t.botMode[mode]}
          </label>
        ))}
      </fieldset>
      <ErrorMessage error={error} />
      {saved ? <p role="status">{t.ui.saved}</p> : null}
      {leftLive ? <p role="status">{t.ui.leftLiveNotice}</p> : null}
      <BusyButton type="submit" busy={busy} busyText={t.ui.saving} disabled={!dirty}>
        {t.ui.save}
      </BusyButton>
    </form>
  );
}
