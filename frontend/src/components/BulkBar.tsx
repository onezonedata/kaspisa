"use client";

import { useLayoutEffect, useState } from "react";

import { bulkUpdate, type ApiError } from "@/api/client";
import type { BulkChanges } from "@/api/types";
import { fill, parseErrorText, t } from "@/i18n";
import { parseIntField } from "@/lib/parse";
import { ErrorMessage } from "./ErrorMessage";
import { PasswordConfirm } from "./PasswordConfirm";

// Границ цены в массовой правке нет: тип BulkChanges их не содержит (ADR 0010).
const TOGGLES: { label: string; changes: Partial<BulkChanges> }[] = [
  { label: t.ui.bulkTrackOn, changes: { tracked: true } },
  { label: t.ui.bulkTrackOff, changes: { tracked: false } },
  { label: t.ui.bulkDecreaseOn, changes: { auto_decrease: true } },
  { label: t.ui.bulkDecreaseOff, changes: { auto_decrease: false } },
  { label: t.ui.bulkRaiseOn, changes: { auto_raise: true } },
  { label: t.ui.bulkRaiseOff, changes: { auto_raise: false } },
];

export function BulkBar({
  shopId,
  selectedIds,
  onDone,
}: {
  shopId: string;
  selectedIds: number[];
  onDone: (updated: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [step, setStep] = useState("");
  const [stepError, setStepError] = useState<string | null>(null);
  // Правка, ждущая подтверждения пароля.
  const [ask, setAsk] = useState<Partial<BulkChanges> | null>(null);

  useLayoutEffect(
    () => () => {
      setError(null);
      setAsk(null);
    },
    [],
  );

  async function apply(changes: Partial<BulkChanges>) {
    setBusy(true);
    setError(null);
    setAsk(null);
    try {
      const result = await bulkUpdate(shopId, selectedIds, changes);
      onDone(result.updated);
    } catch (e) {
      const apiError = e as ApiError;
      // Не ошибка, а просьба ввести пароль; после ввода правка повторяется один раз (ADR 0024).
      if (apiError.code === "password_required") setAsk(changes);
      else setError(apiError);
    } finally {
      setBusy(false);
    }
  }

  function applyStep() {
    const parsed = parseIntField(step, { optional: false });
    if (!parsed.ok) {
      setStepError(parseErrorText(parsed));
      return;
    }
    setStepError(null);
    void apply({ step: parsed.value as number });
  }

  return (
    <section className="bulk-bar">
      <p className="bulk-count">{fill(t.ui.bulkSelected, { n: selectedIds.length })}</p>
      {TOGGLES.map(({ label, changes }) => (
        <button key={label} type="button" disabled={busy} onClick={() => void apply(changes)}>
          {label}
        </button>
      ))}
      <div className="bulk-step">
        <label>
          {t.ui.bulkStepLabel}
          <input inputMode="numeric" value={step} onChange={(e) => setStep(e.target.value)} />
        </label>
        <button type="button" disabled={busy} onClick={applyStep}>
          {t.ui.bulkStepApply}
        </button>
      </div>
      {stepError !== null ? <p role="alert">{stepError}</p> : null}
      {error?.status === 404 ? <p role="alert">{t.ui.bulkNotFound}</p> : <ErrorMessage error={error} />}
      {ask !== null ? (
        <PasswordConfirm onConfirmed={() => void apply(ask)} onCancel={() => setAsk(null)} />
      ) : null}
    </section>
  );
}
